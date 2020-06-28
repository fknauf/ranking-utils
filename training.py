import csv
import os

import torch
from torch.utils.data import DataLoader
from tqdm import tqdm

from qa_utils.cached_dataset import DatasetCache, PairwiseDatasetCache
from qa_utils.io import list_to, list_or_tensor_to
from qa_utils.misc import Logger


def save_args(args_file, args):
    """Save all arguments in a file.

    Arguments:
        args_file {str} -- The csv file to save
        args {argparse.Namespace} -- Command line arguments
    """
    print('writing {}...'.format(args_file))
    with open(args_file, 'w', newline='\n') as fp:
        writer = csv.writer(fp)
        for arg in vars(args):
            writer.writerow([arg, getattr(args, arg)])


def save_checkpoint(state, epoch, ckpt_dir):
    """Takes a dict containing training state and saves it to a checkpoint file in ckpt_dir.

    Arguments:
        state {dict} -- dict containing checkpoint information e.g. pytorch state dicts of model and optimizer.
        epoch {int} -- epoch at which the state was taken.
        ckpt_dir {str} -- path to checkpoint directory.
    """
    fname = os.path.join(ckpt_dir, 'weights_{:03d}.pt'.format(epoch))
    torch.save(state, fname)


def prepare_logging(args):
    """Creates the checkpoint directory, a logger and exports the training arguments.

    Arguments:
        args {argparse.Namespace} -- All command line arguments

    Returns:
        tuple[misc.Logger, str] -- A logger and the working directory
    """
    ckpt_dir = os.path.join(args.working_dir, 'ckpt')
    log_file = os.path.join(args.working_dir, 'train.csv')
    os.makedirs(ckpt_dir, exist_ok=True)
    logger = Logger(log_file, ['epoch', 'loss'])

    args_file = os.path.join(args.working_dir, 'args.csv')
    save_args(args_file, args)

    return logger, ckpt_dir


def train_model_bce(model, train_dl, optimizer, args, device, has_multiple_inputs=False):
    """Train a model using binary cross entropy. Save the model after each epoch and log the loss in
    a file.

    Arguments:
        model {torch.nn.Module} -- The model to train
        train_dl {torch.utils.data.DataLoader} -- Train dataloader
        optimizer {torch.optim.Optimizer} -- Optimizer
        args {argparse.Namespace} -- All command line arguments
        device {torch.device} -- Device to train on

    Keyword Arguments:
        has_multiple_inputs {bool} -- Whether the input is a a list of tensors (default: {False})
    """
    logger, ckpt_dir = prepare_logging(args)

    criterion = torch.nn.BCEWithLogitsLoss()
    model.train()
    for epoch in range(args.epochs):
        optimizer.zero_grad()
        loss_sum = 0
        for i, (b_x, b_y) in enumerate(tqdm(train_dl, desc='epoch {}'.format(epoch))):
            if has_multiple_inputs:
                inputs = list_to(device, b_x)
                out = model(*inputs)
            else:
                out = model(b_x.to(device))
            loss = criterion(out, b_y.to(device)) / args.accumulate_batches
            loss_sum += loss.item()
            loss.backward()
            if (i + 1) % args.accumulate_batches == 0:
                optimizer.step()
                optimizer.zero_grad()

        epoch_loss = loss_sum / len(train_dl)
        logger.log([epoch, epoch_loss])

        state = {'epoch': epoch, 'batch': i, 'state_dict': model.module.state_dict(),
                 'optimizer': optimizer.state_dict()}
        save_checkpoint(state, epoch, ckpt_dir)


def train_model_pairwise(model, criterion, train_dl, optimizer, args, device,
                         num_neg_examples, has_multiple_inputs=False):
    """Train a model using a pairwise ranking loss. For each positive example, calculate the loss
    with a number of negative examples and update the weights only using the highest loss.

    Arguments:
        model {torch.nn.Module} -- The model to train
        criterion {function} -- Pairwise loss function
        train_dl {torch.utils.data.DataLoader} -- Train DataLoader
        optimizer {torch.optim.Optimizer} -- Optimizer
        args {argparse.Namespace} -- All command line arguments
        device {torch.device} -- Device to train on
        num_neg_examples {int} -- Number of negative examples per query

    Keyword Arguments:
        has_multiple_inputs {bool} -- Whether the input is a a list of tensors (default: {False})
    """
    logger, ckpt_dir = prepare_logging(args)

    model.train()
    for epoch in range(args.epochs):
        optimizer.zero_grad()
        loss_sum = 0
        for i, batch in enumerate(tqdm(train_dl, desc='epoch {}'.format(epoch))):
            pos_inputs, neg_inputs = batch

            pos_inputs = list_or_tensor_to(device, pos_inputs)
            neg_inputs = [list_or_tensor_to(device, x) for x in neg_inputs]

            max_neg_inputs = _get_max_loss_neg_batch(model, criterion, pos_inputs, neg_inputs,
                                                     num_neg_examples, has_multiple_inputs)

            if has_multiple_inputs:
                pos_scores = model(*pos_inputs)
                neg_scores = model(*max_neg_inputs)
            else:
                pos_scores = model(pos_inputs)
                neg_scores = model(max_neg_inputs)

            loss = criterion(pos_scores, neg_scores)
            loss = torch.mean(loss)
            loss = loss / args.accumulate_batches
            loss_sum += loss.item()

            loss.backward()
            if (i + 1) % args.accumulate_batches == 0:
                optimizer.step()
                optimizer.zero_grad()

        epoch_loss = loss_sum / len(train_dl)
        logger.log([epoch, epoch_loss])

        state = {'epoch': epoch, 'batch': i, 'state_dict': model.module.state_dict(),
                 'optimizer': optimizer.state_dict()}
        save_checkpoint(state, epoch, ckpt_dir)


def _get_max_loss_neg_batch(model, criterion, pos_inputs, neg_inputs, num_neg_examples,
                            has_multiple_inputs):
    """Helper function for finding negative examples with highest pairwise loss from a list of
    batches with negative input examples. If the inputs are sequences, all batches are assumed to be
    padded to the same length.

    Arguments:
        model {torch.nn.Module} -- Model for computing the pairwise loss based on its predictions
        criterion {function} -- Pairwise loss function
        pos_inputs {torch.Tensor or list[torch.Tensor]} -- Batch of positive examples
        neg_inputs {list[torch.Tensor] or list[list[torch.Tensor]]} -- Batches of negative examples
        num_neg_examples {int} -- Number of negative examples per query
        has_multiple_inputs {bool} -- Whether the input is a a list of tensors

    Returns:
        list[torch.Tensor] -- Negative examples with maximum loss along dimension 1
    """
    with torch.no_grad():
        if has_multiple_inputs:
            pos_scores = model(*pos_inputs)
        else:
            pos_scores = model(pos_inputs)
        # to be able to compute loss for negative positive docs in parallel we repeat the pos scores
        # since they're the same for each negative doc
        pos_scores = pos_scores.repeat_interleave(num_neg_examples, dim=0)

        if has_multiple_inputs:
            neg_scores = model(*[torch.cat(x) for x in neg_inputs])
        else:
            neg_scores = model(torch.cat(neg_inputs))

        losses = criterion(pos_scores, neg_scores).squeeze(1)
        # split the tensor in tuples corresponding to each positive example
        losses = torch.split(losses, num_neg_examples)

        # index of the highest loss negative inputs for each row in the batch
        losses = torch.stack(losses)
        max_loss_ids = torch.argmax(losses, dim=1)

        # we return the inputs corresponding to the IDs with the highest losses
        if has_multiple_inputs:
            max_neg_inputs = [[] for _ in range(len(neg_inputs))]
            for i in range(len(neg_inputs)):
                for j, idx in enumerate(max_loss_ids):
                    max_neg_inputs[i].append(neg_inputs[i][j][idx])
            return [torch.stack(x) for x in max_neg_inputs]

        max_neg_inputs = []
        for i, idx in enumerate(max_loss_ids):
            max_neg_inputs.append(neg_inputs[i][idx])
        return torch.stack(max_neg_inputs)
