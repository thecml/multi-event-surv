import numpy as np
import os
import argparse
import pandas as pd
import config as cfg
from pycox.evaluation import EvalSurv
import torch
from utility.tuning import *
from sota_models import *
from utility.data import dotdict
import data_loader
from utility.survival import make_time_bins, preprocess_data, convert_to_structured
from utility.survival import make_time_bins_hierarchical, digitize_and_convert
from utility.data import calculate_vocab_size, format_data_for_survtrace
from utility.mtlr import mtlr, train_mtlr_model, make_mtlr_prediction
from utility.evaluation import LifelinesEvaluator
from survtrace.model import SurvTraceMulti
from survtrace.train_utils import Trainer
from utility.config import load_config
from hierarchical import util
from utility.hierarch import format_hyperparams, get_layer_size_fine_bins

import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)

os.environ["WANDB_SILENT"] = "true"
import wandb

N_RUNS = 1
PROJECT_NAME = "mensa_multi"

#DATASETS = ["seer"] #"mimic", "als", "rotterdam"
#MODELS = ["cox", "coxboost", "rsf", "mtlr"]

# Setup device
device = "cpu" # use CPU
device = torch.device(device)

def main():
    global model_name
    global dataset_name
    
    """
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', type=str,
                        required=True,
                        default=None)
    parser.add_argument('--model', type=str,
                        required=True,
                        default=None)
    args = parser.parse_args()
    
    if args.dataset:
        dataset_name = args.dataset
    if args.model:
        model_name = args.model
    """
    
    model_name = "direct"
    dataset_name = "als"
    
    # Direct, Hierarch
    if model_name == "direct":
        sweep_config = get_direct_sweep_cfg()
    elif model_name == "hierarch":
        sweep_config = get_hierarch_sweep_cfg()
    else:
        raise ValueError("Model not found")
    
    sweep_id = wandb.sweep(sweep_config, project=f'{PROJECT_NAME}_{model_name}')
    wandb.agent(sweep_id, train_deepsurv_model, count=N_RUNS)

def train_deepsurv_model():
    if model_name == "direct":
        config_defaults = cfg.DIRECT_FULL_PARAMS
    elif model_name == "hierarch":
        config_defaults = cfg.HIERARCH_FULL_PARAMS
    else:
        raise ValueError("Model not found")
    
    # Initialize a new wandb run
    wandb.init(config=config_defaults, group=dataset_name)
    config = wandb.config
    
    # Load data
    if dataset_name == "seer":
        dl = data_loader.SeerDataLoader().load_data()
    elif dataset_name == "mimic":
        dl = data_loader.MimicDataLoader().load_data()
    elif dataset_name == "als":
        dl = data_loader.ALSDataLoader().load_data()
    elif dataset_name == "rotterdam":
        dl = data_loader.RotterdamDataLoader().load_data()
    else:
        raise ValueError("Dataset not found")
    
    num_features, cat_features = dl.get_features()
    data = dl.get_data()
    
    # Calculate time bins
    time_bins = make_time_bins(data[1], event=data[2][:,0])
    
    # Split data
    train_data, valid_data, test_data = dl.split_data(train_size=0.6, valid_size=0.5)
    train_data = [train_data[0][:1000], train_data[1][:1000], train_data[2][:1000]]
    valid_data = [valid_data[0][:1000], valid_data[1][:1000], valid_data[2][:1000]]
    test_data = [test_data[0][:1000], test_data[1][:1000], test_data[2][:1000]]
    n_events = dl.n_events

    # Impute and scale data
    train_data[0], valid_data[0], test_data[0] = preprocess_data(train_data[0], valid_data[0], test_data[0],
                                                                 cat_features, num_features, as_array=True)

    # Train model
    if model_name in ["direct", "hierarch"]:
        data_settings = load_config(cfg.DATASET_CONFIGS_DIR, f"{dataset_name}.yaml")
        num_bins = data_settings['num_bins']
        train_event_bins = make_time_bins_hierarchical(train_data[1], num_bins=num_bins)
        valid_event_bins = make_time_bins_hierarchical(valid_data[1], num_bins=num_bins)
        train_data_hierarch = [train_data[0], train_event_bins, train_data[2]]
        valid_data_hierarch = [valid_data[0], valid_event_bins, valid_data[2]]
        model_config = dotdict(config)
        model_config['layer_size_fine_bins'] = get_layer_size_fine_bins(dataset_name)
        hyperparams = format_hyperparams(model_config)
        verbose = False
        model = util.get_model_and_output(model_name, train_data_hierarch, valid_data_hierarch,
                                          valid_data_hierarch, data_settings, hyperparams, verbose)
    else:
        raise ValueError("Model not found")
    
    ci_results = list()
    for event_id in range(n_events):
        # Compute survival function
        if model_name in ["direct", "hierarch"]:
            surv_preds = util.get_surv_curves(torch.Tensor(valid_data_hierarch[0]), model)
            y_train_time = train_event_bins[:,event_id]
            y_train_event = train_data[2][:,event_id]
            y_valid_time = valid_event_bins[:,event_id]
            y_valid_event = valid_data[2][:,event_id]
            surv_pred_event = pd.DataFrame(surv_preds[event_id])
            lifelines_eval = LifelinesEvaluator(surv_pred_event.T, y_valid_time, y_valid_event,
                                                y_train_time, y_train_event)
        else:
            raise ValueError("Model not found")
            
        # Compute CI
        ci_results.append(lifelines_eval.concordance()[0])
        
    # Log to wandb
    wandb.log({"val_ci": np.mean(ci_results)})
    
if __name__ == "__main__":
    main()