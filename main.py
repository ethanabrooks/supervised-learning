from __future__ import print_function

import argparse
import itertools
import random
from collections import namedtuple, Counter
from pathlib import Path

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from rl_utils import hierarchical_parse_args
from tensorboardX import SummaryWriter
from torch.utils.data import DataLoader, random_split
from torchvision import transforms
from tqdm import tqdm

from datasets import AddLabel, NoiseDataset
from networks import Classifier, Discriminator
from util import get_n_gpu, is_correct, binary_is_correct

Datasets = namedtuple("Datasets", "train test valid")
Networks = namedtuple("Networks", "classifier discriminator")


def train(
    classifier, discriminator, alpha, device, train_loader, optimizer, log_interval
):
    classifier.train()
    counter = Counter()
    for batch_idx, (data, target) in tqdm(
        enumerate(train_loader), total=len(train_loader), desc="classifier"
    ):
        data = data.to(device)
        target = Networks(*[t.to(device) for t in target])
        target = target._replace(
            discriminator=target.discriminator.unsqueeze(1).float()
        )
        optimizer.zero_grad()
        classifier_output, *activations = classifier(data)
        discriminator_output = discriminator(*activations)
        loss = Networks(
            classifier=F.nll_loss(classifier_output, target.classifier),
            discriminator=F.binary_cross_entropy_with_logits(
                discriminator_output, target.discriminator
            ),
        )
        (loss.classifier - alpha * loss.discriminator).backward()
        optimizer.step()
        counter.update(
            classifier_train_loss=loss.classifier.item(),
            classifier_train_accuracy=is_correct(classifier_output, target.classifier),
            discriminator_loss_on_classifier=loss.discriminator.item(),
            discriminator_accuracy_on_classifier=binary_is_correct(
                discriminator_output, target.discriminator
            ),
            batch=1,
            total=len(data),
        )
        if batch_idx % log_interval == 0:
            N = counter.pop("total")
            yield {k: v if k == "batch" else v / N for k, v in counter.items()}
            counter = Counter()
    #     print(
    #     "Train Batch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}".format(
    #         batch_idx,
    #         batch_idx * len(data),
    #         len(train_loader.dataset),
    #         100.0 * batch_idx / len(train_loader),
    #         loss.classifier.item(),
    #         )
    # )

    # correct += is_correct(output, target)
    # total += target.numel()
    # if batch_idx % log_interval == 0:
    #     idx = i * len(train_loader) + batch_idx
    #
    #     tick = time.time()
    #     writer.add_scalar("fps", (tick - start) / log_interval, idx)
    #     start = tick
    #
    #     writer.add_scalar("loss", loss.item(), idx)
    #     writer.add_scalar("train accuracy", correct / total, idx)


def test(classifier, device, test_loader):
    classifier.eval()
    counter = Counter()
    with torch.no_grad():
        for data, (target, _) in test_loader:
            data, target = data.to(device), target.to(device)
            output, *_ = classifier(data)
            counter.update(
                classifier_test_loss=F.nll_loss(output, target, reduction="sum").item(),
                classifier_test_accuracy=is_correct(output, target),
                total=target.numel(),
            )  # sum up batch loss

    N = counter.pop("total")
    correct = counter["classifier_test_accuracy"]
    print(
        "\nTest set: Average loss: {:.4f}, Accuracy: {}/{} ({:.0f}%)\n".format(
            counter["classifier_test_loss"] / N, correct, N, 100.0 * correct
        )
    )
    return {k: v / N for k, v in counter.items() if k != "total"}


def train_discriminator(
    classifier, discriminator, device, train_loader, optimizer, i, log_interval, writer
):
    classifier.eval()
    counter = Counter()
    for batch_idx, (data, (_, target)) in tqdm(
        enumerate(train_loader), total=len(train_loader), desc="discriminator"
    ):
        data = data.to(device)
        target = target.to(device).unsqueeze(1).float()
        optimizer.zero_grad()
        classifier_output, *activations = classifier(data)
        discriminator_output = discriminator(*activations)
        loss = F.binary_cross_entropy_with_logits(discriminator_output, target)
        loss.backward()
        optimizer.step()
        counter.update(
            discriminator_train_loss=loss.item(),
            discriminator_train_accuracy=binary_is_correct(
                discriminator_output, target
            ),
            total=len(data),
            batch=1,
        )
        if batch_idx % log_interval == 0:
            # print(
            # "Discriminator Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}".format(
            # i,
            # batch_idx * len(data),
            # len(train_loader.dataset),
            # 100.0 * batch_idx / len(train_loader),
            # loss.item(),
            # )
            # )
            N = counter.pop("total")
            yield {k: v if k == "batch" else v / N for k, v in counter.items()}
            counter = Counter()


def test_discriminator(classifier, discriminator, device, test_loader, i, writer):
    classifier.eval()
    counter = Counter()
    with torch.no_grad():
        for data, (_, target) in test_loader:
            data = data.to(device)
            target = target.to(device).unsqueeze(1).float()
            classifier_output, *activations = classifier(data)
            discriminator_output = discriminator(*activations)
            logits = F.binary_cross_entropy_with_logits(discriminator_output, target)
            counter.update(
                discriminator_test_loss=logits.item(),
                discriminator_test_accuracy=binary_is_correct(
                    discriminator_output, target
                ),
                total=target.numel(),
            )
    N = counter.pop("total")
    print(
        "\nTest set: average loss: {:.4f}, Accuracy: {}/{} ({:.0f}%)\n".format(
            counter["discriminator_test_loss"] / N,
            counter["discriminator_test_accuracy"],
            N,
            100.0 * counter["discriminator_test_accuracy"] / N,
        )
    )
    return {k: v / N for k, v in counter.items()}


def main(
    no_cuda,
    seed,
    batch_size,
    percent_noise,
    random_labels,
    classifier_optimizer_args,
    classifier_epochs,
    discriminator_optimizer_args,
    discriminator_epochs,
    discriminator_args,
    classifier_load_path,
    log_dir,
    log_interval,
    run_id,
    num_iterations,
    alpha,
):
    use_cuda = not no_cuda and torch.cuda.is_available()
    torch.manual_seed(1)
    if use_cuda:
        n_gpu = get_n_gpu()
        try:
            index = int(run_id[-1])
        except ValueError:
            index = random.randrange(0, n_gpu)
        device = torch.device("cuda", index=index % n_gpu)
    else:
        device = "cpu"
    kwargs = {"num_workers": 1, "pin_memory": True, "shuffle": True} if use_cuda else {}

    train_dataset = NoiseDataset(
        "../data",
        train=True,
        download=True,
        transform=transforms.Compose(
            [transforms.ToTensor(), transforms.Normalize((0.1307,), (0.3081,))]
        ),
        percent_noise=percent_noise,
    )
    test_dataset = NoiseDataset(
        "../data",
        train=False,
        transform=transforms.Compose(
            [transforms.ToTensor(), transforms.Normalize((0.1307,), (0.3081,))]
        ),
        percent_noise=percent_noise,
    )
    size = len(train_dataset) + len(test_dataset)
    splits = Datasets(train=size * 3 // 7, test=size * 3 // 7, valid=size * 1 // 7)
    classifier_datasets = Datasets(
        *[
            AddLabel(dataset, label, random_labels=random_labels)
            for label, dataset in enumerate(
                random_split(train_dataset + test_dataset, splits)
            )
        ]
    )
    classifier_loaders = Datasets(
        *[
            DataLoader(dataset, batch_size=batch_size, **kwargs)
            for dataset in classifier_datasets
        ]
    )
    discriminator_dataset = Datasets(
        *random_split(
            classifier_datasets.train + classifier_datasets.test,
            [splits.train, splits.test],
        ),
        valid=None,
    )
    discriminator_loaders = Datasets(
        train=DataLoader(discriminator_dataset.train, batch_size=batch_size, **kwargs),
        test=DataLoader(discriminator_dataset.test, batch_size=batch_size, **kwargs),
        valid=None,
    )
    classifier = Classifier().to(device)
    classifier_optimizer = optim.SGD(
        classifier.parameters(),
        **{
            k.replace("classifier_", ""): v
            for k, v in classifier_optimizer_args.items()
        },
    )
    discriminator = Discriminator(**discriminator_args).to(device)
    discriminator_optimizer = optim.SGD(
        discriminator.parameters(),
        **{
            k.replace("discriminator_", ""): v
            for k, v in discriminator_optimizer_args.items()
        },
    )
    writer = SummaryWriter(str(log_dir))
    if classifier_load_path:
        classifier.load_state_dict(torch.load(classifier_load_path))
        # sanity check to make sure that classifier was properly loaded
        for k, v in test(
            classifier=classifier, device=device, test_loader=classifier_loaders.train
        ).items():
            writer.add_scalar(k, v, 0)
        torch.manual_seed(seed)
    else:
        torch.manual_seed(seed)
        iterations = range(num_iterations) if num_iterations else itertools.count()
        batch_count = Counter()

        for i in iterations:
            for k, v in test(
                classifier=classifier,
                device=device,
                test_loader=classifier_loaders.valid,
            ).items():
                writer.add_scalar(k, v, i)
            for epoch in range(1, classifier_epochs + 1):
                for counter in train(
                    classifier=classifier,
                    discriminator=discriminator,
                    alpha=alpha if i > 0 else 0,
                    device=device,
                    train_loader=classifier_loaders.train,
                    optimizer=classifier_optimizer,
                    log_interval=log_interval,
                ):
                    batch_count.update(classifier=counter["batch"])
                    for k, v in counter.items():
                        if k != "batch":
                            writer.add_scalar(k, v, batch_count["classifier"])
            for k, v in test_discriminator(
                classifier=classifier,
                discriminator=discriminator,
                device=device,
                test_loader=discriminator_loaders.test,
                i=i,
                writer=writer,
            ).items():
                writer.add_scalar(k, v, i)
            for epoch in range(1, discriminator_epochs + 1):
                for j, counter in enumerate(
                    train_discriminator(
                        classifier=classifier,
                        discriminator=discriminator,
                        device=device,
                        train_loader=discriminator_loaders.train,
                        optimizer=discriminator_optimizer,
                        i=i * discriminator_epochs + epoch,
                        log_interval=log_interval,
                        writer=writer,
                    )
                ):
                    batch_count.update(discriminator=counter["batch"])
                    for k, v in counter.items():
                        if k != "batch":
                            writer.add_scalar(k, v, batch_count["discriminator"])
            torch.save(classifier.state_dict(), str(Path(log_dir, "mnist_cnn.pt")))


def cli():
    # Training settings
    parser = argparse.ArgumentParser(description="PyTorch MNIST Example")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=64,
        metavar="N",
        help="input batch size for training (default: 64)",
    )
    parser.add_argument("--percent-noise", type=float, required=True, metavar="N")
    parser.add_argument("--num-iterations", type=int, metavar="N")
    parser.add_argument(
        "--classifier-epochs",
        type=int,
        default=20,
        metavar="N",
        help="number of epochs to train (default: 10)",
    )
    parser.add_argument(
        "--discriminator-epochs",
        type=int,
        default=20,
        metavar="N",
        help="number of epochs to train (default: 10)",
    )
    parser.add_argument("--alpha", type=float, default=0.1, metavar="N")
    discriminator_parser = parser.add_argument_group("discriminator_args")
    discriminator_parser.add_argument(
        "--hidden-size", type=int, default=512, metavar="N"
    )
    discriminator_parser.add_argument("--num-hidden", type=int, default=1, metavar="N")
    discriminator_parser.add_argument(
        "--activation", type=lambda s: eval(f"nn.{s}"), default=nn.ReLU(), metavar="N"
    )
    discriminator_parser.add_argument("--dropout", action="store_true")
    classifier_optimizer_parser = parser.add_argument_group("classifier_optimizer_args")
    classifier_optimizer_parser.add_argument(
        "--classifier-lr",
        type=float,
        default=0.01,
        metavar="LR",
        help="learning rate (default: 0.01)",
    )
    classifier_optimizer_parser.add_argument(
        "--classifier-momentum",
        type=float,
        default=0.5,
        metavar="M",
        help="SGD momentum (default: 0.5)",
    )
    discriminator_optimizer_parser = parser.add_argument_group(
        "discriminator_optimizer_args"
    )
    discriminator_optimizer_parser.add_argument(
        "--discriminator-lr",
        type=float,
        default=0.01,
        metavar="LR",
        help="learning rate (default: 0.01)",
    )
    discriminator_optimizer_parser.add_argument(
        "--discriminator-momentum",
        type=float,
        default=0.5,
        metavar="M",
        help="SGD momentum (default: 0.5)",
    )
    parser.add_argument("--random-labels", action="store_true")
    parser.add_argument(
        "--no-cuda", action="store_true", default=False, help="disables CUDA training"
    )
    parser.add_argument(
        "--seed", type=int, default=1, metavar="S", help="random seed (default: 1)"
    )
    parser.add_argument(
        "--log-interval",
        type=int,
        default=10,
        metavar="N",
        help="how many batches to wait before logging training status",
    )
    parser.add_argument("--log-dir", default="/tmp/mnist", metavar="N")
    parser.add_argument("--run-id", metavar="N", default="")
    parser.add_argument("--classifier-load-path")
    main(**hierarchical_parse_args(parser))


if __name__ == "__main__":
    cli()
