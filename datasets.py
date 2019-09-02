import torch
from torch.utils.data import Dataset, ConcatDataset
from torchvision import datasets


class NoiseDataset(datasets.MNIST):
    def __init__(self, *args, percent_noise=1, **kwargs):
        super().__init__(*args, **kwargs)
        self.percent_noise = percent_noise
        self.noise = torch.randn(self.data.shape)

    def __getitem__(self, index):
        x, y = super().__getitem__(index)
        noise = self.noise[index]
        x = noise * self.percent_noise + x * (1 - self.percent_noise)
        return x, y


class AddLabel(Dataset):
    def __init__(self, dataset, extra_label, random_labels=False):
        self.label = extra_label
        self.dataset = dataset
        self.random_labels = (
            torch.randint(low=0, high=2, size=(len(dataset),))
            if random_labels
            else None
        )

    def __getitem__(self, item):
        x, y = self.dataset[item]
        try:
            label = self.random_labels[item]
        except TypeError:
            label = self.label
        return x, (y, label)

    def __add__(self, other):
        return ConcatDataset([self, other])

    def __len__(self):
        return len(self.dataset)
