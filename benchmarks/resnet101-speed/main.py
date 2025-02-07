"""ResNet-101 Speed Benchmark"""
import os
import platform
from tabnanny import check
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, cast

import click
import torch
from torch import nn
import torch.nn.functional as F
from torch.optim import SGD

import torch.distributed as dist
import torch.multiprocessing as mp
from torch.nn.parallel import DistributedDataParallel as DDP

from resnet import resnet101
import torchgpipe
from torchgpipe import GPipe

Stuffs = Tuple[nn.Module, int, List[torch.device]]  # (model, batch_size, devices)
Experiment = Callable[[nn.Module, List[int]], Stuffs]


class Experiments:

    @staticmethod
    def baseline(model: nn.Module, devices: List[int]) -> Stuffs:
        batch_size = 118
        device = devices[0]
        model.to(device)
        return model, batch_size, [torch.device(device)]

    @staticmethod
    def pipeline1(model: nn.Module, devices: List[int]) -> Stuffs:
        batch_size = 220
        chunks = 2
        balance = [370]

        model = cast(nn.Sequential, model)
        model = GPipe(model, balance, devices=devices, chunks=chunks, checkpoint='never')
        return model, batch_size, list(model.devices)

    @staticmethod
    def pipeline2(model: nn.Module, devices: List[int]) -> Stuffs:
        batch_size = 25000
        chunks = 1667
        balance = [135, 235]

        model = cast(nn.Sequential, model)
        model = GPipe(model, balance, devices=devices, chunks=chunks, checkpoint='never')
        return model, batch_size, list(model.devices)

    @staticmethod
    def pipeline4(model: nn.Module, devices: List[int]) -> Stuffs:
        batch_size = 5632
        chunks = 256
        balance = [44, 92, 124, 110]

        model = cast(nn.Sequential, model)
        model = GPipe(model, balance, devices=devices, chunks=chunks, checkpoint='never')
        return model, batch_size, list(model.devices)

    @staticmethod
    def pipeline8(model: nn.Module, devices: List[int]) -> Stuffs:
        batch_size = 5400
        chunks = 150
        balance = [26, 22, 33, 44, 44, 66, 66, 69]

        model = cast(nn.Sequential, model)
        model = GPipe(model, balance, devices=devices, chunks=chunks, checkpoint='never')
        return model, batch_size, list(model.devices)


EXPERIMENTS: Dict[str, Experiment] = {
    'baseline': Experiments.baseline,
    'pipeline-1': Experiments.pipeline1,
    'pipeline-2': Experiments.pipeline2,
    'pipeline-4': Experiments.pipeline4,
    'pipeline-8': Experiments.pipeline8,
}


BASE_TIME: float = 0


def hr() -> None:
    """Prints a horizontal line."""
    width, _ = click.get_terminal_size()
    click.echo('-' * width)


def log(msg: str, clear: bool = False, nl: bool = True) -> None:
    """Prints a message with elapsed time."""
    if clear:
        # Clear the output line to overwrite.
        width, _ = click.get_terminal_size()
        click.echo('\b\r', nl=False)
        click.echo(' ' * width, nl=False)
        click.echo('\b\r', nl=False)

    t = time.time() - BASE_TIME
    h = t // 3600
    t %= 3600
    m = t // 60
    t %= 60
    s = t

    click.echo('%02d:%02d:%02d | ' % (h, m, s), nl=False)
    click.echo(msg, nl=nl)


def parse_devices(ctx: Any, param: Any, value: Optional[str]) -> List[int]:
    if value is None:
        return list(range(torch.cuda.device_count()))
    return [int(x) for x in value.split(',')]


def data_parallel_train(rank, world_size, experiment, devices, epochs):
    """Train ResNet-101 with data parallelism"""
    # initialize environment
    os.environ['MASTER_ADDR'] = 'localhost'
    os.environ['MASTER_PORT'] = '12355'
    dist.init_process_group('nccl', rank=rank, world_size=world_size)

    model: nn.Module = resnet101(num_classes=1000)

    if len(devices) % world_size != 0:
        raise ValueError('GPUs cannot be divided evenly into %d pipelines' % (world_size))
    num_devices_per_pipeline = len(devices) // world_size
    # Get the set of GPUs to be used for this pipeline
    devices_pipeline = devices[rank * num_devices_per_pipeline: (rank+1) * num_devices_per_pipeline]

    # Pipeline parallelism
    f = EXPERIMENTS[experiment]
    try:
        model, batch_size, _devices = f(model, devices_pipeline)
    except ValueError as exc:
        # Examples:
        #   ValueError: too few devices to hold given partitions (devices: 1, paritions: 2)
        raise exc

    # Data parallelism
    model = DDP(model)

    optimizer = SGD(model.parameters(), lr=0.1)

    in_device = _devices[0]
    out_device = _devices[-1]
    torch.cuda.set_device(in_device)

    # This experiment cares about only training speed, rather than accuracy.
    # To eliminate any overhead due to data loading, we use fake random 224x224
    # images over 1000 labels.
    dataset_size = 50000

    input = torch.rand(batch_size, 3, 224, 224, device=in_device)
    target = torch.randint(1000, (batch_size,), device=out_device)
    data = [(input, target)] * (dataset_size//batch_size)

    if dataset_size % batch_size != 0:
        last_input = input[:dataset_size % batch_size]
        last_target = target[:dataset_size % batch_size]
        data.append((last_input, last_target))

    # HEADER ======================================================================================

    title = f'{experiment}, {epochs} epochs'

    if rank == 0:
        # Only print the header on data-parallel rank 0
        click.echo(title)

        if isinstance(model, GPipe):
            click.echo(f'batch size: {batch_size}, chunks: {model.chunks}, balance: {model.balance}')
        else:
            click.echo(f'batch size: {batch_size}')

        click.echo('torchgpipe: %s, python: %s, torch: %s, cudnn: %s, cuda: %s, gpu: %s' % (
            torchgpipe.__version__,
            platform.python_version(),
            torch.__version__,
            torch.backends.cudnn.version(),
            torch.version.cuda,
            torch.cuda.get_device_name(in_device)))

    # TRAIN =======================================================================================

    if rank == 0:
        global BASE_TIME
        BASE_TIME = time.time()

        throughputs = []
        elapsed_times = []

        hr()

    for epoch in range(epochs):
        torch.cuda.synchronize(in_device)

        if rank == 0:
            tick = time.time()

        data_trained = 0
        for i, (input, target) in enumerate(data):
            data_trained += input.size(0)

            output = model(input)
            loss = F.cross_entropy(output, target)
            loss.backward()

            optimizer.step()
            optimizer.zero_grad()

            if rank == 0:
                # 00:01:02 | 1/20 epoch (42%) | 200.000 samples/sec (estimated)
                percent = (i+1) / len(data) * 100
                throughput = data_trained / (time.time()-tick)
                log('%d/%d epoch (%d%%) | %.3f samples/sec (estimated)'
                    '' % (epoch+1, epochs, percent, throughput), clear=True, nl=False)

        torch.cuda.synchronize(in_device)

        if rank == 0:
            tock = time.time()
            
            # 00:02:03 | 1/20 epoch | 200.000 samples/sec, 123.456 sec/epoch
            elapsed_time = tock - tick
            throughput = dataset_size / elapsed_time
            log('%d/%d epoch | %.3f samples/sec, %.3f sec/epoch'
                '' % (epoch+1, epochs, throughput, elapsed_time), clear=True)
            
            throughputs.append(throughput)
            elapsed_times.append(elapsed_time)

    # RESULT ======================================================================================
    if rank == 0:
        hr()
    
        # pipeline-4, 2-10 epochs | 200.000 samples/sec, 123.456 sec/epoch (average)
        n = len(throughputs)
        throughput = sum(throughputs) / n
        elapsed_time = sum(elapsed_times) / n
        click.echo('%s | %.3f samples/sec, %.3f sec/epoch (average)'
                '' % (title, throughput, elapsed_time))


@click.command()
@click.pass_context
@click.argument(
    'experiment',
    type=click.Choice(sorted(EXPERIMENTS.keys())),
)
@click.option(
    '--dp_world_size', '-dp',
    type=int,
    default=1,
    help='World size of data parallelism (default: 1)',
)
@click.option(
    '--epochs', '-e',
    type=int,
    default=10,
    help='Number of epochs (default: 10)',
)
@click.option(
    '--devices', '-d',
    metavar='0,1,2,3',
    callback=parse_devices,
    help='Device IDs to use (default: all CUDA devices)',
)
def cli(ctx: click.Context,
        experiment: str,
        dp_world_size: int,
        epochs: int,
        devices: List[int],
        ) -> None:
    """ResNet-101 Speed Benchmark"""
    mp.spawn(data_parallel_train, args=(dp_world_size, experiment, devices, epochs), 
            nprocs=dp_world_size, join=True)


if __name__ == '__main__':
    cli()
