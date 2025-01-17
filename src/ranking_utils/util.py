import csv
import pickle
import tempfile
from pathlib import Path
from collections import defaultdict
from typing import Dict, Iterable, List, Tuple

import h5py
import torch
from tqdm import tqdm
from pytorch_lightning import Trainer

from ranking_utils.lightning.base_ranker import BaseRanker
from ranking_utils.lightning.datasets import ValTestDatasetBase


def predict_and_save(trainer: Trainer, test_ds: ValTestDatasetBase):
    """Predict and save predictions in a file. The file is created in the `log_dir` of the trainer.
    Original query and document IDs are recovered and written in the files.
    The file name is unique w.r.t. `trainer.local_rank`.

    Args:
        trainer (Trainer): Trainer object with associated model
        test_ds (ValTestDatasetBase): Test dataset used to recover original IDs
    """
    out_dict = defaultdict(list)
    for item in trainer.predict():
        out_dict["q_ids"].extend(
            map(test_ds.get_original_query_id, item["q_ids"].tolist())
        )
        out_dict["doc_ids"].extend(
            map(test_ds.get_original_document_id, item["doc_ids"].tolist())
        )
        out_dict["predictions"].extend(item["predictions"].tolist())

    f = Path(trainer.log_dir) / f"predictions_{trainer.local_rank}.pkl"
    with open(f, "wb") as fp:
        pickle.dump(out_dict, fp)


def read_predictions(files: Iterable[Path]) -> Dict[str, Dict[str, float]]:
    """Read and combine predictions from .pkl files.

    Args:
        files (Iterable[Path]): All files to read

    Returns:
        Dict[str, Dict[str, float]]: Query IDs mapped to document IDs mapped to scores
    """
    result = defaultdict(dict)
    for f in files:
        with open(f, "rb") as fp:
            d = pickle.load(fp)
            for q_id, doc_id, prediction in zip(
                d["q_ids"], d["doc_ids"], d["predictions"]
            ):
                result[q_id][doc_id] = prediction
    return result


def write_trec_eval_file(
    out_file: Path, predictions: Dict[str, Dict[str, float]], name: str
):
    """Write the results in a file accepted by the TREC evaluation tool.

    Args:
        out_file (Path): File to create
        predictions (Dict[str, Dict[str, float]]): Query IDs mapped to document IDs mapped to scores
        name (str): Method name
    """
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w", encoding="utf-8", newline="") as fp:
        writer = csv.writer(fp, delimiter="\t")
        for q_id in predictions:
            ranking = sorted(
                predictions[q_id].keys(), key=predictions[q_id].get, reverse=True
            )
            for rank, doc_id in enumerate(ranking, 1):
                score = predictions[q_id][doc_id]
                writer.writerow([q_id, "Q0", doc_id, rank, score, name])


def create_temp_testsets(
    data_file: Path, runfiles: Iterable[Path]
) -> List[Tuple[int, str]]:
    """Create re-ranking testsets in a temporary files.

    Args:
        data_file (Path): Pre-processed data file containing queries and documents
        runfiles (Iterable[Path]): Runfiles to create testsets for (TREC format)

    Returns:
        List[Tuple[int, str]]: Descriptors and paths of the temporary files
    """
    # recover the internal integer query and doc IDs
    int_q_ids = {}
    int_doc_ids = {}
    with h5py.File(data_file, "r") as fp:
        for int_id, orig_id in enumerate(
            tqdm(fp["orig_q_ids"].asstr(), total=len(fp["orig_q_ids"]))
        ):
            int_q_ids[orig_id] = int_id
        for int_id, orig_id in enumerate(
            tqdm(fp["orig_doc_ids"].asstr(), total=len(fp["orig_doc_ids"]))
        ):
            int_doc_ids[orig_id] = int_id

    result = []
    for runfile in runfiles:
        qd_pairs = []
        with open(runfile) as fp:
            for line in fp:
                q_id, _, doc_id, _, _, _ = line.split()
                qd_pairs.append((q_id, doc_id))
        fd, f = tempfile.mkstemp()
        with h5py.File(f, "w") as fp:
            num_items = len(qd_pairs)
            ds = {
                "q_ids": fp.create_dataset("q_ids", (num_items,), dtype="int32"),
                "doc_ids": fp.create_dataset("doc_ids", (num_items,), dtype="int32"),
                "labels": fp.create_dataset("labels", (num_items,), dtype="int32"),
            }
            for i, (q_id, doc_id) in enumerate(tqdm(qd_pairs, desc="Saving testset")):
                ds["q_ids"][i] = int_q_ids[q_id]
                ds["doc_ids"][i] = int_doc_ids[doc_id]
        result.append((fd, f))
    return result


def rank(
    model: BaseRanker,
    testset: ValTestDatasetBase,
    batch_size: int,
    num_workers: int = 16,
) -> Dict[str, Dict[str, float]]:
    """Rank all query-document pairs in a testset using a trained ranking model.

    Args:
        model (BaseRanker): The ranking model
        testset (ValTestDatasetBase): Testset with query-document pairs to rank
        batch_size (int): Batch size
        num_workers (int, optional): Number of DataLoader workers. Defaults to 16.

    Returns:
        Dict[str, Dict[str, float]]: Query IDs mapped to document IDs mapped to scores
    """
    if torch.cuda.is_available():
        print("CUDA available")
        model = torch.nn.DataParallel(model)
        dev = "cuda:0"
    else:
        print("CUDA unavailable")
        dev = "cpu"
    model.to(dev)
    model.eval()

    dl = torch.utils.data.DataLoader(
        testset,
        batch_size=batch_size,
        num_workers=num_workers,
        collate_fn=testset.collate_fn,
    )
    result = defaultdict(dict)
    for q_ids, doc_ids, inputs, _ in tqdm(dl):
        with torch.no_grad():
            inputs = [i.to(dev) for i in inputs]
            outputs = model(inputs)
        for q_id, doc_id, prediction in zip(q_ids, doc_ids, outputs):
            orig_q_id = testset.get_original_query_id(q_id.cpu())
            orig_doc_id = testset.get_original_document_id(doc_id.cpu())
            prediction = prediction.detach().cpu().numpy()[0]
            result[orig_q_id][orig_doc_id] = prediction
    return result
