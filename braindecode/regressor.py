# Authors: Maciej Sliwowski <maciek.sliwowski@gmail.com>
#          Robin Schirrmeister <robintibor@gmail.com>
#          Lukas Gemein <l.gemein@gmail.com>
#
# License: BSD (3-clause)

import warnings

import numpy as np
from sklearn.metrics import get_scorer
from skorch.callbacks import EpochTimer, BatchScoring, PrintLog, EpochScoring
from skorch.classifier import NeuralNet
from skorch.regressor import NeuralNetRegressor
from skorch.utils import train_loss_score, valid_loss_score, noop, to_numpy
import torch

from .training.scoring import (PostEpochTrainScoring,
                               CroppedTrialEpochScoring,
                               CroppedTimeSeriesEpochScoring,
                               predict_trials)
from .util import ThrowAwayIndexLoader, update_estimator_docstring


class EEGRegressor(NeuralNetRegressor):
    doc = """Regressor that calls loss function directly.

    Parameters
    ----------
    cropped: bool (default=False)
        Defines whether torch model passed to this class is cropped or not.
        Currently used for callbacks definition.

    callbacks: None or list of strings or list of Callback instances (default=None)
        More callbacks, in addition to those returned by
        ``get_default_callbacks``. Each callback should inherit from
        :class:`skorch.callbacks.Callback`. If not ``None``, callbacks can be a
        list of strings specifying `sklearn` scoring functions (for scoring
        functions names see: https://scikit-learn.org/stable/modules/model_evaluation.html#scoring-parameter)
        or a list of callbacks where the callback names are inferred from the
        class name. Name conflicts are resolved by appending a count suffix
        starting with 1, e.g. ``EpochScoring_1``. Alternatively,
        a tuple ``(name, callback)`` can be passed, where ``name``
        should be unique. Callbacks may or may not be instantiated.
        The callback name can be used to set parameters on specific
        callbacks (e.g., for the callback with name ``'print_log'``, use
        ``net.set_params(callbacks__print_log__keys_ignored=['epoch',
        'train_loss'])``).

    iterator_train__shuffle: bool (default=True)
        Defines whether train dataset will be shuffled. As skorch does not
        shuffle the train dataset by default this one overwrites this option.

    aggregate_predictions: bool (default=True)
        Whether to average cropped predictions to obtain window predictions. Used only in the
        cropped mode.

    """  # noqa: E501
    __doc__ = update_estimator_docstring(NeuralNetRegressor, doc)

    def __init__(self, *args, cropped=False, callbacks=None,
                 iterator_train__shuffle=True, aggregate_predictions=True, **kwargs):
        self.cropped = cropped
        self.aggregate_predictions = aggregate_predictions
        self._last_window_inds_ = None
        super().__init__(*args,
                         callbacks=callbacks,
                         iterator_train__shuffle=iterator_train__shuffle,
                         **kwargs)

    def _yield_callbacks(self):
        # Here we parse the callbacks supplied as strings,
        # e.g. 'accuracy', to the callbacks skorch expects
        for name, cb, named_by_user in super()._yield_callbacks():
            if name == 'str':
                train_cb, valid_cb = self._parse_str_callback(cb)
                yield train_cb
                if self.train_split is not None:
                    yield valid_cb
            else:
                yield name, cb, named_by_user

    def _parse_str_callback(self, cb_supplied_name):
        scoring = get_scorer(cb_supplied_name)
        scoring_name = scoring._score_func.__name__
        assert scoring_name.endswith(
                        ('_score', '_error', '_deviance', '_loss'))
        if (scoring_name.endswith('_score') or
                cb_supplied_name.startswith('neg_')):
            lower_is_better = False
        else:
            lower_is_better = True
        train_name = f'train_{cb_supplied_name}'
        valid_name = f'valid_{cb_supplied_name}'
        if self.cropped:
            # TODO: use CroppedTimeSeriesEpochScoring when time series target
            # In case of cropped decoding we are using braindecode
            # specific scoring created for cropped decoding
            train_scoring = CroppedTrialEpochScoring(
                cb_supplied_name, lower_is_better, on_train=True, name=train_name
            )
            valid_scoring = CroppedTrialEpochScoring(
                cb_supplied_name, lower_is_better, on_train=False, name=valid_name
            )
        else:
            train_scoring = PostEpochTrainScoring(
                cb_supplied_name, lower_is_better, name=train_name
            )
            valid_scoring = EpochScoring(
                cb_supplied_name, lower_is_better, on_train=False, name=valid_name
            )
        named_by_user = True
        train_valid_callbacks = [
            (train_name, train_scoring, named_by_user),
            (valid_name, valid_scoring, named_by_user)
        ]
        return train_valid_callbacks

    # pylint: disable=arguments-differ
    def get_loss(self, y_pred, y_true, *args, **kwargs):
        """Return the loss for this batch by calling NeuralNet get_loss.

        Parameters
        ----------
        y_pred : torch tensor
            Predicted target values
        y_true : torch tensor
            True target values.
        X : input data, compatible with skorch.dataset.Dataset
            By default, you should be able to pass:

                * numpy arrays
                * torch tensors
                * pandas DataFrame or Series
                * scipy sparse CSR matrices
                * a dictionary of the former three
                * a list/tuple of the former three
                * a Dataset

            If this doesn't work with your data, you have to pass a
            ``Dataset`` that can deal with the data.
        training : bool (default=False)
            Whether train mode should be used or not.

        Returns
        -------
        loss : float
            The loss value.
        """
        return NeuralNet.get_loss(self, y_pred, y_true, *args, **kwargs)

    def get_iterator(self, dataset, training=False, drop_index=True):
        iterator = super().get_iterator(dataset, training=training)
        if drop_index:
            return ThrowAwayIndexLoader(self, iterator, is_regression=True)
        else:
            return iterator

    def on_batch_end(self, net, *batch, training=False, **kwargs):
        # If training is false, assume that our loader has indices for this
        # batch
        if not training:
            epoch_cbs = []
            for name, cb in self.callbacks_:
                if isinstance(cb, (CroppedTrialEpochScoring, CroppedTimeSeriesEpochScoring)) and (
                        hasattr(cb, 'window_inds_')) and (not cb.on_train):
                    epoch_cbs.append(cb)
            # for trialwise decoding stuffs it might also be we don't have
            # cropped loader, so no indices there
            if len(epoch_cbs) > 0:
                assert self._last_window_inds_ is not None
                for cb in epoch_cbs:
                    cb.window_inds_.append(self._last_window_inds_)
                self._last_window_inds_ = None

    def predict_with_window_inds_and_ys(self, dataset):
        self.module.eval()
        preds = []
        i_window_in_trials = []
        i_window_stops = []
        window_ys = []
        for X, y, i in self.get_iterator(dataset, drop_index=False):
            i_window_in_trials.append(i[0].cpu().numpy())
            i_window_stops.append(i[2].cpu().numpy())
            with torch.no_grad():
                preds.append(to_numpy(self.module.forward(X.to(self.device))))
            window_ys.append(y.cpu().numpy())
        preds = np.concatenate(preds)
        i_window_in_trials = np.concatenate(i_window_in_trials)
        i_window_stops = np.concatenate(i_window_stops)
        window_ys = np.concatenate(window_ys)
        return dict(
            preds=preds, i_window_in_trials=i_window_in_trials,
            i_window_stops=i_window_stops, window_ys=window_ys)

    # Removes default EpochScoring callback computing 'accuracy' to work properly
    # with cropped decoding.
    @property
    def _default_callbacks(self):
        return [
            ("epoch_timer", EpochTimer()),
            (
                "train_loss",
                BatchScoring(
                    train_loss_score,
                    name="train_loss",
                    on_train=True,
                    target_extractor=noop,
                ),
            ),
            (
                "valid_loss",
                BatchScoring(
                    valid_loss_score, name="valid_loss", target_extractor=noop,
                ),
            ),
            ("print_log", PrintLog()),
        ]

    # Method added to fix `predict_proba` behavior to return proper values
    # in cropped mode even if regressor does not return probabilities.
    # We implement `predict_proba` because it exists in skorch.NeuralNetRegressor.
    def predict_proba(self, X):
        """Return the output of the module's forward method as a numpy
        array. In case of cropped decoding returns averaged values for
        each trial.

        If the module's forward method returns multiple outputs as a
        tuple, it is assumed that the first output contains the
        relevant information and the other values are ignored.
        If all values are relevant or module's output for each crop
        is needed, consider using :func:`~skorch.NeuralNet.forward`
        instead.

        Parameters
        ----------
        X : input data, compatible with skorch.dataset.Dataset
          By default, you should be able to pass:

            * numpy arrays
            * torch tensors
            * pandas DataFrame or Series
            * scipy sparse CSR matrices
            * a dictionary of the former three
            * a list/tuple of the former three
            * a Dataset

          If this doesn't work with your data, you have to pass a
          ``Dataset`` that can deal with the data.

        Warnings
        --------
        Regressors predict regression targets, so output of this method
        can't be interpreted as probabilities. We advise you to use
        `predict` method instead of `predict_proba`.

        Returns
        -------
        y_proba : numpy ndarray

        """
        y_pred = super().predict_proba(X)
        # Normally, we have to average the predictions across crops/timesteps
        # to get one prediction per window/trial
        # Predictions may be already averaged in CroppedTrialEpochScoring (y_pred.shape==2).
        # However, when predictions are computed outside of CroppedTrialEpochScoring
        # we have to average predictions, hence the check if len(y_pred.shape) == 3
        if self.cropped and self.aggregate_predictions and len(y_pred.shape) == 3:
            return y_pred.mean(-1)
        else:
            return y_pred

    def predict_trials(self, X, return_targets=True):
        """Create trialwise predictions and optionally also return trialwise
        labels from cropped dataset.

        Parameters
        ----------
        X: braindecode.datasets.BaseConcatDataset
            A braindecode dataset to be predicted.
        return_targets: bool
            If True, additionally returns the trial targets.

        Returns
        -------
        trial_predictions: np.ndarray
            3-dimensional array (n_trials x n_classes x n_predictions), where
            the number of predictions depend on the chosen window size and the
            receptive field of the network.
        trial_labels: np.ndarray
            2-dimensional array (n_trials x n_targets) where the number of
            targets depends on the decoding paradigm and can be either a single
            value, multiple values, or a sequence.
        """
        if not self.cropped:
            warnings.warn(
                "This method was designed to predict trials in cropped mode. "
                "Calling it when cropped is False will give the same result as "
                "'.predict'.", UserWarning)
            preds = self.predict(X)
            if return_targets:
                return preds, np.concatenate([X[i][1] for i in range(len(X))])
            return preds
        return predict_trials(
            module=self.module,
            dataset=X,
            return_targets=return_targets,
            batch_size=self.batch_size,
            num_workers=self.get_iterator(X, training=False).loader.num_workers,
        )

    def fit(self, X, y, **kwargs):
        if y is not None:
            if y.ndim == 1:
                y = np.array(y).reshape(-1, 1)
        super().fit(X=X, y=y, **kwargs)
