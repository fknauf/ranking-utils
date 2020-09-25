from pathlib import Path
from collections import defaultdict
from typing import Any, Dict, Optional, Tuple

import abc
import torch
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler
from pytorch_lightning import LightningModule, TrainResult, EvalResult

from qa_utils.lightning.sampler import DistributedQuerySampler
from qa_utils.lightning.datasets import TrainDatasetBase, ValTestDatasetBase
from qa_utils.lightning.metrics import average_precision, reciprocal_rank


# input batches vary for each model, hence we use Any here
InputBatch = Any
TrainingBatch = Tuple[InputBatch, InputBatch]
ValTestBatch = Tuple[torch.IntTensor, torch.IntTensor, InputBatch, torch.IntTensor]


class BaseRanker(LightningModule, abc.ABC):
    """Abstract base class for re-rankers. Implements average precision and reciprocal rank validation.
    This class needs to be extended and (at least) the following methods must be implemented:
        * forward
        * configure_optimizers

    Since this class uses custom sampling in DDP mode, the `Trainer` object must be initialized using
    `replace_sampler_ddp=False` and the argument `uses_ddp=True` must be set when DDP is active.

    Args:
        hparams (Dict[str, Any]): All model hyperparameters
        train_ds (TrainDatasetBase): The training dataset
        val_ds (Optional[ValTestDatasetBase]): The validation dataset
        test_ds (Optional[ValTestDatasetBase]): The testing dataset
        loss_margin (float): Margin used in pairwise loss
        batch_size (int): The batch size
        validation (str, optional): Metric used for validation. Defaults to 'map'.
        rr_k (int, optional): Compute RR@K. Defaults to 10.
        num_workers (int, optional): Number of DataLoader workers. Defaults to 16.
        uses_ddp (bool, optional): Whether DDP is used. Defaults to False.
    """
    def __init__(self, hparams: Dict[str, Any],
                 train_ds: TrainDatasetBase, val_ds: Optional[ValTestDatasetBase], test_ds: Optional[ValTestDatasetBase],
                 loss_margin: float,
                 batch_size: int, validation: str = 'map', rr_k: int = 10,
                 num_workers: int = 16, uses_ddp: bool = False):
        super().__init__()
        self.hparams = hparams
        self.save_hyperparameters(hparams)

        self.train_ds = train_ds
        self.val_ds = val_ds
        self.test_ds = test_ds
        self.loss_margin = loss_margin
        self.batch_size = batch_size
        self.validation = validation
        self.rr_k = rr_k
        self.num_workers = num_workers
        self.uses_ddp = uses_ddp

    def train_dataloader(self) -> DataLoader:
        """Return a trainset DataLoader. If the trainset object has a function named `collate_fn`,
        it is used. If the model is trained in DDP mode, the standard `DistributedSampler` is used.

        Returns:
            DataLoader: The DataLoader
        """
        if self.uses_ddp:
            sampler = DistributedSampler(self.train_ds, shuffle=True)
            shuffle = None
        else:
            sampler = None
            shuffle = True

        return DataLoader(self.train_ds, batch_size=self.batch_size, sampler=sampler, shuffle=shuffle,
                          num_workers=self.num_workers, collate_fn=getattr(self.train_ds, 'collate_fn', None))

    def val_dataloader(self) -> Optional[DataLoader]:
        """Return a validationset DataLoader if the validationset exists. If the validationset object has a function
        named `collate_fn`, it is used. If the model is validated in DDP mode, `DistributedQuerySampler` is used
        for ranking metrics to work on a query level.

        Returns:
            Optional[DataLoader]: The DataLoader, or None if there is no validation dataset
        """
        if self.val_ds is None:
            return None

        if self.uses_ddp:
            sampler = DistributedQuerySampler(self.val_ds)
        else:
            sampler = None

        return DataLoader(self.val_ds, batch_size=self.batch_size, sampler=sampler, shuffle=False,
                          num_workers=self.num_workers, collate_fn=getattr(self.val_ds, 'collate_fn', None))

    def test_dataloader(self) -> Optional[DataLoader]:
        """Return a testset DataLoader if the testset exists. If the testset object has a function
        named `collate_fn`, it is used. If the model is tested in DDP mode, the standard `DistributedSampler` is used.

        Returns:
            Optional[DataLoader]: The DataLoader, or None if there is no testing dataset
        """
        if self.test_ds is None:
            return None

        if self.uses_ddp:
            sampler = DistributedSampler(self.test_ds, shuffle=False)
        else:
            sampler = None

        return DataLoader(self.test_ds, batch_size=self.batch_size, sampler=sampler, shuffle=False,
                          num_workers=self.num_workers, collate_fn=getattr(self.test_ds, 'collate_fn', None))

    def training_step(self, batch: TrainingBatch, batch_idx: int) -> TrainResult:
        """Train a single batch using a pairwise ranking loss.

        Args:
            batch (TrainingBatch): A pairwise training batch (positive and negative inputs)
            batch_idx (int): Batch index

        Returns:
            TrainResult: Training loss
        """
        pos_inputs, neg_inputs = batch
        pos_outputs = torch.sigmoid(self(pos_inputs))
        neg_outputs = torch.sigmoid(self(neg_inputs))
        loss = torch.mean(torch.clamp(self.loss_margin - pos_outputs + neg_outputs, min=0))
        result = TrainResult(minimize=loss)
        result.log('train_loss', loss, sync_dist=True, sync_dist_op='mean')
        return result

    def validation_step(self, batch: ValTestBatch, batch_idx: int) -> EvalResult:
        """Process a single validation batch.

        Args:
            batch (ValTestBatch): Query IDs, document IDs, inputs and labels
            batch_idx (int): Batch index

        Returns:
            EvalResult: Query IDs, resulting predictions and labels
        """
        q_ids, _, inputs, labels = batch
        outputs = self(inputs)

        # this seems to break DP mode (for now), as lightning always tries to take the mean of lists/tensors
        result = EvalResult()
        result.log('q_ids', q_ids, logger=False, on_step=False, on_epoch=False, reduce_fx=None, sync_dist=False)
        result.log('predictions', outputs, logger=False, on_step=False, on_epoch=False, reduce_fx=None, sync_dist=False)
        result.log('labels', labels, logger=False, on_step=False, on_epoch=False, reduce_fx=None, sync_dist=False)
        return result

    def test_step(self, batch: ValTestBatch, batch_idx: int) -> EvalResult:
        """Process a single test batch. The resulting query IDs, predictions and labels are written to files.
        In DDP mode one file for each device is created. The files are created in the `save_dir` of the logger.

        Args:
            batch (ValTestBatch): Query IDs, document IDs, inputs and labels
            batch_idx (int): Batch index

        Returns:
            EvalResult: The result object that creates the files
        """
        q_ids, doc_ids, inputs, labels = batch
        out_dict = {
            'q_id': [self.test_ds.orig_q_ids[q_id.cpu()] for q_id in q_ids],
            'doc_id': [self.test_ds.orig_doc_ids[doc_id.cpu()] for doc_id in doc_ids],
            'prediction': self(inputs),
            'label': labels
        }
        save_dir = Path(self.logger.save_dir)
        result = EvalResult()
        result.write_dict(out_dict, str(save_dir / 'test_outputs.pt'))
        return result

    def validation_epoch_end(self, val_results: EvalResult) -> EvalResult:
        """Accumulate all validation batches and compute MAP and MRR@k. The results are approximate in DDP mode.

        Args:
            val_results (EvalResult): Query IDs, resulting predictions and labels

        Returns:
            EvalResult: MAP and MRR@k
        """
        temp = defaultdict(lambda: ([], []))
        for q_id, (pred,), label in zip(val_results['q_ids'], val_results['predictions'], val_results['labels']):
            # q_id is a tensor with one element, we convert it to an int to use it as dict key
            q_id = int(q_id.cpu())
            temp[q_id][0].append(pred)
            temp[q_id][1].append(label)

        aps, rrs = [], []
        for predictions, labels in temp.values():
            predictions = torch.stack(predictions)
            labels = torch.stack(labels)
            aps.append(average_precision(predictions, labels))
            rrs.append(reciprocal_rank(predictions, labels, self.rr_k))
        val_map = torch.mean(torch.stack(aps))
        val_mrr = torch.mean(torch.stack(rrs))

        val_metric = val_map if self.validation == 'map' else val_mrr
        result = EvalResult(checkpoint_on=val_metric, early_stop_on=val_metric)
        result.log('val_map', val_map, sync_dist=True, sync_dist_op='mean')
        result.log('val_mrr', val_mrr, sync_dist=True, sync_dist_op='mean')
        return result
