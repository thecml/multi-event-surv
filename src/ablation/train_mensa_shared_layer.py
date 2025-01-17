"""
run_real_multi_event.py
====================================
Models: ['deepsurv', 'hierarch', 'mensa']
"""

# 3rd party
import pandas as pd
import numpy as np
import sys, os
sys.path.append(os.path.abspath('../'))

import config as cfg
import torch
import random
import warnings
import argparse
from SurvivalEVAL.Evaluator import LifelinesEvaluator

# Local
from utility.survival import (make_time_bins, preprocess_data)
from utility.config import load_config
from utility.evaluation import global_C_index, local_C_index
from mensa.model import MENSA

# SOTA
from data_loader import get_data_loader

warnings.filterwarnings("ignore", message=".*The 'nopython' keyword.*")

np.random.seed(0)
torch.manual_seed(0)
torch.cuda.manual_seed_all(0)
random.seed(0)

# Set precision
dtype = torch.float64
torch.set_default_dtype(dtype)

# Setup device
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--use_shared', action='store_true') # store_true = False by default
    parser.add_argument('--dataset_name', type=str, default='proact_me')
    
    args = parser.parse_args()
    seed = args.seed
    use_shared = args.use_shared
    dataset_name = args.dataset_name
    
    # Load and split data
    dl = get_data_loader(dataset_name)
    dl = dl.load_data()
    trajectories = dl.trajectories
    
    train_dict, valid_dict, test_dict = dl.split_data(train_size=0.7, valid_size=0.1, test_size=0.2,
                                                        random_state=seed)
    n_events = dl.n_events
    
    # Preprocess data
    cat_features = dl.cat_features
    num_features = dl.num_features
    event_cols = [f'e{i+1}' for i in range(n_events)]
    time_cols = [f't{i+1}' for i in range(n_events)]
    X_train = pd.DataFrame(train_dict['X'], columns=dl.columns)
    X_valid = pd.DataFrame(valid_dict['X'], columns=dl.columns)
    X_test = pd.DataFrame(test_dict['X'], columns=dl.columns)
    X_train, X_valid, X_test= preprocess_data(X_train, X_valid, X_test, cat_features,
                                            num_features, as_array=True)
    train_dict['X'] = torch.tensor(X_train, device=device, dtype=dtype)
    train_dict['E'] = torch.tensor(train_dict['E'], device=device, dtype=torch.int64)
    train_dict['T'] = torch.tensor(train_dict['T'], device=device, dtype=torch.int64)
    valid_dict['X'] = torch.tensor(X_valid, device=device, dtype=dtype)
    valid_dict['E'] = torch.tensor(valid_dict['E'], device=device, dtype=torch.int64)
    valid_dict['T'] = torch.tensor(valid_dict['T'], device=device, dtype=torch.int64)
    test_dict['X'] = torch.tensor(X_test, device=device, dtype=dtype)
    test_dict['E'] = torch.tensor(test_dict['E'], device=device, dtype=torch.int64)
    test_dict['T'] = torch.tensor(test_dict['T'], device=device, dtype=torch.int64)
    
    n_samples = train_dict['X'].shape[0]
    n_features = train_dict['X'].shape[1]
    
    # Make time bins
    time_bins = make_time_bins(train_dict['T'].cpu(), event=None, dtype=dtype).to(device)
    time_bins = torch.cat((torch.tensor([0]).to(device), time_bins))
    
    # Make model
    config = load_config(cfg.MENSA_CONFIGS_DIR, f"{dataset_name.partition('_')[0]}.yaml")
    n_epochs = config['n_epochs']
    n_dists = config['n_dists']
    lr = config['lr']
    batch_size = config['batch_size']
    layers = config['layers']
    if use_shared:
        model = MENSA(n_features, layers=layers, n_events=n_events,
                      n_dists=n_dists, trajectories=trajectories,
                      device=device)
    else:
        model = MENSA(n_features, layers=layers, n_events=n_events,
                      n_dists=n_dists, use_shared=False,
                      trajectories=trajectories, device=device)
    
    # Train model
    model.fit(train_dict, valid_dict, learning_rate=lr, n_epochs=n_epochs,
                patience=10, batch_size=batch_size, verbose=True)
    
    # Make predictions
    all_preds = []
    for i in range(n_events):
        model_preds = model.predict(test_dict['X'].to(device), time_bins, risk=i+1)
        model_preds = pd.DataFrame(model_preds, columns=time_bins.cpu().numpy())
        all_preds.append(model_preds)
    
    # Calculate local and global CI
    all_preds_arr = [df.to_numpy() for df in all_preds]
    global_ci = global_C_index(all_preds_arr, test_dict['T'].cpu().numpy(),
                            test_dict['E'].cpu().numpy())
    local_ci = local_C_index(all_preds_arr, test_dict['T'].cpu().numpy(),
                            test_dict['E'].cpu().numpy())
    
    # Make evaluation for each event
    model_results = pd.DataFrame()
    for event_id, surv_pred in enumerate(all_preds):
        n_train_samples = len(train_dict['X'])
        n_test_samples= len(test_dict['X'])
        y_train_time = train_dict['T'][:,event_id]
        y_train_event = train_dict['E'][:,event_id]
        y_test_time = test_dict['T'][:,event_id]
        y_test_event = test_dict['E'][:,event_id]
        
        lifelines_eval = LifelinesEvaluator(surv_pred.T, y_test_time, y_test_event,
                                            y_train_time, y_train_event)
        
        ci = lifelines_eval.concordance()[0]
        ibs = lifelines_eval.integrated_brier_score()
        mae = lifelines_eval.mae(method="Margin")
        d_calib = lifelines_eval.d_calibration()[0]
        
        metrics = [ci, ibs, mae, d_calib, global_ci, local_ci]
        print(metrics)
        
        if use_shared:
            model_name = "with_shared"
        else:
            model_name = "no_shared"
        
        res_sr = pd.Series([model_name, dataset_name, seed, event_id+1] + metrics,
                            index=["ModelName", "DatasetName", "Seed", "EventId",
                                "CI", "IBS", "MAE", "DCalib", "GlobalCI", "LocalCI"])
        model_results = pd.concat([model_results, res_sr.to_frame().T], ignore_index=True)
        
    # Save results
    filename = f"{cfg.RESULTS_DIR}/shared_layer.csv"
    if os.path.exists(filename):
        results = pd.read_csv(filename)
    else:
        results = pd.DataFrame(columns=model_results.columns)
    results = results.append(model_results, ignore_index=True)
    results.to_csv(filename, index=False)
        