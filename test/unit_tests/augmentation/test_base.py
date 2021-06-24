# Authors: Cédric Rommel <cpe.rommel@gmail.com>
#
# License: BSD (3-clause)

from functools import partial

import pytest
import numpy as np
from sklearn.utils import check_random_state
import torch
import mne

from braindecode.augmentation.base import (
    Transform, Compose, AugmentedDataLoader
)
from braindecode.datautil import create_from_mne_epochs


def dummy_k_operation(X, y, k):
    return torch.ones_like(X) * k, y


@pytest.fixture
def dummy_transform():
    k = np.random.randint(10)
    return Transform(operation=partial(dummy_k_operation, k=k), probability=1)


def common_tranform_assertions(input_batch, output_batch, expected_X=None):
    """ Assert whether shapes and devices are conserved. Also, (optional)
    checks whether the expected features matrix is produced.

    Parameters
    ----------
    input_batch : tuple
        The batch given to the transform containing a tensor X, of shape
        (batch_sizze, n_channels, sequence_len), and a tensor  y of shape
        (batch_size).
    output_batch : tuple
        The batch output by the transform. Should have two elements: the
        transformed X and y.
    expected_X : tensor, optional
        The expected first element of output_batch, which will be compared to
        it. By default None.
    """
    X, y = input_batch
    tr_X, tr_y = output_batch
    assert tr_X.shape == X.shape
    assert tr_X.shape[0] == tr_y.shape[0]
    assert torch.equal(tr_y, y)
    assert X.device == tr_X.device
    if expected_X is not None:
        assert torch.equal(tr_X, expected_X)


def test_transform_call_with_no_label(random_batch, dummy_transform):
    X, y = random_batch
    tr_X1, _ = dummy_transform(X, y)
    tr_X2 = dummy_transform(X)
    assert torch.equal(tr_X1, tr_X2)


@pytest.mark.parametrize("k1,k2,expected,p1,p2", [
    (1, 0, 0, 1, 1),  # replace by 1s with p=1, then 0s with p=1 -> 0s
    (0, 1, 1, 1, 1),  # replace by 0s with p=1, then 1s with p=1 -> 1s
    (1, 0, 1, 1, 0),  # replace by 1s with p=1, then 1s with p=0 -> 1s
    (0, 1, 0, 1, 0),  # replace by 0s with p=1, then 0s with p=0 -> 0s
    (1, 0, 0, 0, 1),  # replace by 1s with p=0, then 0s with p=1 -> 0s
    (0, 1, 1, 0, 1),  # replace by 0s with p=0, then 1s with p=1 -> 1s
])
def test_transform_composition(random_batch, k1, k2, expected, p1, p2):
    X, y = random_batch
    dummy_transform1 = Transform(partial(dummy_k_operation, k=k1), p1)
    dummy_transform2 = Transform(partial(dummy_k_operation, k=k2), p2)
    concat_transform = Compose([dummy_transform1, dummy_transform2])
    expected_tensor = torch.ones(
        X.shape,
        device=X.device
    ) * expected

    common_tranform_assertions(
        random_batch,
        concat_transform(X, y),
        expected_tensor
    )


def test_transform_proba_exception(rng_seed, dummy_transform):
    rng = check_random_state(rng_seed)
    with pytest.raises(AssertionError):
        Transform(
            operation=dummy_transform,
            probability='a',
            random_state=rng,
        )


@pytest.fixture(scope="session")
def concat_windows_dataset():
    """Generates a small BaseConcatDataset out of WindowDatasets extracted
    from the physionet database.
    """
    subject_id = 22
    event_codes = [5, 6, 9, 10, 13, 14]
    physionet_paths = mne.datasets.eegbci.load_data(
        subject_id, event_codes, update_path=False)

    parts = [mne.io.read_raw_edf(path, preload=True, stim_channel='auto')
             for path in physionet_paths]
    list_of_epochs = [mne.Epochs(raw, [[0, 0, 0]], tmin=0, baseline=None)
                      for raw in parts]
    windows_datasets = create_from_mne_epochs(
        list_of_epochs,
        window_size_samples=50,
        window_stride_samples=50,
        drop_last_window=False
    )

    return windows_datasets


# test AugmentedDataLoader with 0, 1 and 2 composed transforms
@pytest.mark.parametrize("nb_transforms,no_list", [
    (0, False), (1, False), (1, True), (2, False)
])
def test_data_loader(dummy_transform, concat_windows_dataset, nb_transforms,
                     no_list):
    transforms = [dummy_transform for _ in range(nb_transforms)]
    if no_list:
        transforms = transforms[0]
    data_loader = AugmentedDataLoader(
        concat_windows_dataset,
        transforms=transforms,
        batch_size=128)
    for idx_batch, _ in enumerate(data_loader):
        if idx_batch >= 3:
            break


def test_data_loader_exception(concat_windows_dataset):
    with pytest.raises(TypeError):
        AugmentedDataLoader(
            concat_windows_dataset,
            transforms='a',
            batch_size=128
        )
