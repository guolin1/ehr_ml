import os
import tqdm
import torch
import random
import logging

import numpy as np
from collections import defaultdict

from .dataset import DataLoader
from .opt import OpenAIAdam
from .prediction_model import PredictionModel
from .utils import read_config, read_info, device_from_config

from .. import timeline
from .. import ontology
from .. import labeler

from ..utils import set_up_logging
from ..extension.clmbr import PatientTimelineDataset

from typing import Dict, Any, Union, List, Tuple, Optional

class CLMBRFeaturizer:
    def __init__(self,
                 config: Dict[Any, Any],
                 info: Dict[Any, Any],
                 device: torch.device = torch.device('cpu'),
                 log_path: Optional[str] = None):
        if log_path is not None:
            set_up_logging(log_path)
        self.model = PredictionModel(config, info).to(device)

    def _build_adam_optimizer(self,
                              dataset: PatientTimelineDataset) -> OpenAIAdam:
        config = self.model.config
        params = []
        for name, param in self.model.named_parameters():
            if not param.requires_grad:
                continue
            params.append(param)
        optimizer = OpenAIAdam(
            params,
            lr=config["lr"],
            schedule="warmup_linear",
            warmup=config["warmup_epochs"] / config["epochs_per_cycle"],
            t_total=dataset.num_batches(config["batch_size"], False)
            * config["epochs_per_cycle"],
            b1=config["b1"],
            b2=config["b2"],
            e=config["e"],
            l2=config["l2"],
        )
        logging.info(f"Batches per epoch = {dataset.num_batches(config['batch_size'], False)}")
        logging.info(f"Total batches = {optimizer.defaults['t_total']}")
        return optimizer

    def _train_epoch(self, dataset: PatientTimelineDataset,
                     pbar: Optional[tqdm.tqdm] = None) -> None:
        self.model.train()
        total_non_text_loss = 0
        config = self.model.config
        with DataLoader(
                dataset,
                threshold=config["num_first"],
                is_val=False,
                batch_size=config["batch_size"],
                seed=random.randint(0, 100000),
                day_dropout=config["day_dropout"],
                code_dropout=config["code_dropout"],
        ) as batches:
            for i, batch in enumerate(batches):
                values, non_text_loss = self.model(batch)
                del values

                self.optimizer.zero_grad()
                non_text_loss.backward()
                self.optimizer.step()

                del non_text_loss
                del batch
                if pbar is not None:
                    pbar.update(1)
                elif i % 2000 == 0:
                    logging.info(f"Seen batch {i}")
    
    def fit(self, dataset: PatientTimelineDataset, use_pbar: bool = True) -> None:
        self.model.train()
        
        model_dir = self.model.config["model_dir"]
        num_epochs = self.model.config["epochs_per_cycle"]
        
        self.optimizer = self._build_adam_optimizer(dataset)

        checkpoint_dir = os.path.join(model_dir, 'checkpoints')
        os.makedirs(checkpoint_dir, exist_ok=True)
        
        best_val_loss = None
        best_val_loss_epoch = None

        pbar = tqdm.tqdm(total = self.optimizer.defaults['t_total']) if use_pbar \
                         else None
        loss_file = open(os.path.join(model_dir, "losses"), "w")

        logging.info("Start training")
        for epoch in range(num_epochs):
            logging.info("About to start epoch %s", epoch)
            if pbar is not None:
                pbar.set_description(f"Epoch {epoch}")
            self._train_epoch(dataset, pbar = pbar)
            logging.info("Epoch %s is complete", epoch)

            if pbar is not None:
                pbar.set_description(f"Evaluating epoch {epoch}")
            train_loss = self.evaluate(dataset, is_val=False, num_batches = 2000)
            val_loss = self.evaluate(dataset, is_val=True, num_batches = 2000)

            logging.info("Train loss: %s", train_loss)
            logging.info("Val loss: %s", val_loss)

            loss_file.write("Epoch {}\n".format(epoch))
            loss_file.write("Train loss {}\n".format(train_loss))
            loss_file.write("Val loss {}\n".format(val_loss))
            loss_file.write("\n")
            loss_file.flush()

            if best_val_loss is None or val_loss < best_val_loss:
                best_val_loss = val_loss
                best_val_loss_epoch = epoch

                if os.path.exists(os.path.join(model_dir, "best")):
                    os.unlink(os.path.join(model_dir, "best"))

                torch.save(self.model.state_dict(), os.path.join(model_dir, "best"))
                logging.info("Saving best model to %s", os.path.join(model_dir, "best"))

        if pbar is not None:
            pbar.close()
        loss_file.close()
        logging.info("Training complete!")
        
    def evaluate(self,
                 dataset: PatientTimelineDataset,
                 is_val: bool = True,
                 num_batches: Optional[int] = None) -> float:
        self.model.eval()
        config = self.model.config
        if num_batches is None:
            num_batches = dataset.num_batches(config["batch_size"], is_val)
        total_loss = 0
        with DataLoader(
            dataset,
            threshold=config["num_first"],
            is_val=is_val,
            batch_size=config["eval_batch_size"],
            seed=0,
            day_dropout=config["day_dropout"],
            code_dropout=config["code_dropout"],
        ) as batches:
            for batch, _ in zip(batches, range(num_batches)):
                with torch.no_grad():
                    values, non_text_loss = self.model(batch)
                    del values

                    total_loss += non_text_loss.item()

                    del batch
                    del non_text_loss

        return total_loss / num_batches

    def featurize_patients(self,
                           extract_dir: str,
                           patient_ids: Union[List[str], np.array],
                           day_offsets: Union[List[str], np.array]) -> np.array:
        """
        Read info and configuration from a pretrained model dir to load a pretrained CLMBR model
        """
        config = self.model.config
        dummy_labels = [0 for _ in patient_ids]
        data = (dummy_labels, patient_ids, day_offsets)

        dataset = PatientTimelineDataset(
            os.path.join(extract_dir, "extract.db"),
            os.path.join(extract_dir, "ontology.db"),
            os.path.join(config["model_dir"], "info.json"),
            data,
            data,
        )

        patient_id_to_info = defaultdict(dict)
        for i, (pid, index) in enumerate(zip(patient_ids, day_offsets)):
            patient_id_to_info[pid][index] = i

        patient_representations = np.zeros((len(patient_ids), self.model.config["size"]))
        with DataLoader(
                dataset,
                threshold=config["num_first"],
                is_val=True,
                batch_size=config["eval_batch_size"],
                seed=random.randint(0, 100000)
        ) as batches:
            pbar = tqdm.tqdm(batches)
            pbar.set_description("Computing patient representations")
            for batch in pbar:
                with torch.no_grad():
                    embeddings = (
                        self.model.compute_embedding_batch(batch["rnn"]).cpu().numpy()
                    )
                    for i, patient_id in enumerate(batch["pid"]):
                        for index, target_id in patient_id_to_info[patient_id].items():
                            patient_representations[target_id, :] = embeddings[i, index, :]

        return patient_representations

    def featurize_patients_w_labels(
            self,
            extract_dir: str,
            l: labeler.SavedLabeler
    ) -> Tuple[np.array, np.array, np.array, np.array]:
        """
        Featurize patients using the given model and labeler.
        The result is a numpy array aligned with l.get_labeler_data().
        This function will use the GPU if it is available.
        """
        data = l.get_label_data()

        labels, patient_ids, patient_indices = data
        
        patient_representations = self.featurize_patients(
            extract_dir, patient_ids, patient_indices
        )

        return patient_representations, labels, patient_ids, patient_indices
        
    @classmethod
    def from_pretrained(cls, model_dir: str):
        config = read_config(os.path.join(model_dir, "config.json"))
        info = read_info(os.path.join(model_dir, "info.json"))
        device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
        featurizer = cls(config, info, device=device)
        model_data = torch.load(os.path.join(model_dir, "best"), map_location="cpu")
        featurizer.model.load_state_dict(model_data)
        return featurizer
