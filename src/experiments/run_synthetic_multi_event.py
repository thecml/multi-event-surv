"""
run_synthetic_me_three_events.py
====================================
Experiment 3.1

Models: ["deepsurv", 'hierarch', 'mensa', 'dgp']
"""

# 3rd party
import pandas as pd
import numpy as np
import config as cfg
import torch
import torch.optim as optim
import torch.nn as nn
import random
import warnings
import copy
import tqdm
import math
import argparse
from scipy.interpolate import interp1d
from SurvivalEVAL.Evaluator import LifelinesEvaluator

# Local
from data_loader import MultiEventSyntheticDataLoader
from copula import Clayton2D, Frank2D
from dgp import Weibull_linear, Weibull_nonlinear, Weibull_log_linear
from utility.survival import (make_time_bins, preprocess_data, convert_to_structured,
                              risk_fn, compute_l1_difference, predict_survival_function,
                              make_times_hierarchical)
from utility.data import dotdict
from utility.config import load_config
from utility.loss import triple_loss
from mensa.model import train_mensa_model_3_events, make_mensa_model_3_events
from utility.data import format_data_deephit_cr, format_hierarch_data_multi_event, calculate_layer_size_hierarch
from utility.evaluation import global_C_index, local_C_index

# SOTA
from sota_models import (make_cox_model, make_coxnet_model, make_coxboost_model, make_dcph_model,
                          make_deephit_cr, make_dsm_model, make_rsf_model, train_deepsurv_model,
                          make_deepsurv_prediction, DeepSurv, make_deephit_cr, train_deephit_model)
from hierarchical import util
from hierarchical.helper import format_hierarchical_hyperparams

warnings.filterwarnings("ignore", message=".*The 'nopython' keyword.*")

np.random.seed(0)
torch.manual_seed(0)
random.seed(0)

# Set precision
dtype = torch.float64
torch.set_default_dtype(dtype)

# Setup device
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

# Define models
MODELS = ['deepsurv']

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--k_tau', type=float, default=0.25)
    parser.add_argument('--copula_name', type=str, default="clayton")
    parser.add_argument('--linear', type=bool, default=True)
    
    args = parser.parse_args()
    seed = args.seed
    k_tau = args.k_tau
    copula_name = args.copula_name
    linear = args.linear
    
    # Load and split data
    data_config = load_config(cfg.DGP_CONFIGS_DIR, f"synthetic_me.yaml")
    dl = MultiEventSyntheticDataLoader().load_data(data_config, k_taus=[k_tau, k_tau, k_tau],
                                                   linear=linear, device=device, dtype=dtype)
    train_dict, valid_dict, test_dict = dl.split_data(train_size=0.7, valid_size=0.1, test_size=0.2)
    
    n_samples = train_dict['X'].shape[0]
    n_features = train_dict['X'].shape[1]
    n_events = data_config['n_events']
    dgps = dl.dgps

    # Make time bins
    min_time = dl.get_data()[1].min()
    max_time = dl.get_data()[1].max()
    time_bins = make_time_bins(train_dict['T'], event=None, dtype=dtype)
    time_bins = torch.concat([torch.tensor([min_time], device=device, dtype=dtype), 
                              time_bins, torch.tensor([max_time], device=device, dtype=dtype)])
    
    # Evaluate models
    model_results = pd.DataFrame()
    for model_name in MODELS:
        if model_name == "deepsurv":
            config = dotdict(cfg.DEEPSURV_PARAMS)
            trained_models = []
            for i in range(n_events):
                model = DeepSurv(in_features=n_features, config=config)
                data_train = pd.DataFrame(train_dict['X'])
                data_train['time'] = train_dict['T'][:,i]
                data_train['event'] = train_dict['E'][:,i]
                data_valid = pd.DataFrame(valid_dict['X'])
                data_valid['time'] = valid_dict['T'][:,i]
                data_valid['event'] = valid_dict['E'][:,i]
                model = train_deepsurv_model(model, data_train, data_valid, time_bins, config=config,
                                             random_state=0, reset_model=True, device=device, dtype=dtype)
                trained_models.append(model)
        elif model_name == "hierarch":
            config = load_config(cfg.HIERARCH_CONFIGS_DIR, f"synthetic_me.yaml")
            n_time_bins = len(time_bins)
            train_data, valid_data, test_data = format_hierarch_data_multi_event(train_dict, valid_dict,
                                                                                 test_dict, n_time_bins)
            config['min_time'] = int(train_data[1].min())
            config['max_time'] = int(train_data[1].max())
            config['num_bins'] = n_time_bins
            params = cfg.HIERARCH_PARAMS
            params['n_batches'] = int(n_samples/params['batch_size'])
            layer_size = params['layer_size_fine_bins'][0][0]
            params['layer_size_fine_bins'] = calculate_layer_size_hierarch(layer_size, n_time_bins)
            hyperparams = format_hierarchical_hyperparams(params)
            verbose = params['verbose']
            model = util.get_model_and_output("hierarch_full", train_data, test_data,
                                              valid_data, config, hyperparams, verbose)
        elif model_name == "mensa":
            config = load_config(cfg.MENSA_CONFIGS_DIR, f"synthetic.yaml")
            model1, model2, model3, copula = make_mensa_model_3_events(n_features, start_theta=2.0, eps=1e-4,
                                                                       device=device, dtype=dtype)
            model1, model2, model3, copula = train_mensa_model_3_events(train_dict, valid_dict, model1,
                                                                        model2, model3, copula,
                                                                        n_epochs=5000, lr=0.001)
            print(f"NLL all events: {triple_loss(model1, model2, model3, valid_dict, copula)}")
            print(f"DGP loss: {triple_loss(dgps[0], dgps[1], dgps[2], valid_dict, copula)}")
        elif model_name == "dgp":
            continue
        else:
            raise NotImplementedError()
        
        # Compute survival function
        n_samples = test_dict['X'].shape[0]                    
        if model_name == "deepsurv":
            all_preds = []
            for trained_model in trained_models:
                preds, time_bins_model, _ = make_deepsurv_prediction(trained_model, test_dict['X'],
                                                                     config=config, dtype=dtype)
                spline = interp1d(time_bins_model, preds, kind='linear', fill_value='extrapolate')
                preds = pd.DataFrame(spline(time_bins), columns=time_bins.numpy())
                all_preds.append(preds)
        elif model_name == "hierarch":
            event_preds = util.get_surv_curves(test_data[0], model)
            bin_locations = np.linspace(0, config['max_time'], event_preds[0].shape[1])
            all_preds = []
            for event_pred in event_preds:
                preds = pd.DataFrame(event_pred, columns=bin_locations)
                spline = interp1d(bin_locations, preds, kind='linear', fill_value='extrapolate')
                preds = pd.DataFrame(spline(time_bins), columns=time_bins.numpy())
                all_preds.append(preds)
        elif model_name == "mensa":
            preds_e1 = predict_survival_function(model1, test_dict['X'], time_bins).detach().numpy()
            preds_e2 = predict_survival_function(model2, test_dict['X'], time_bins).detach().numpy()
            preds_e3 = predict_survival_function(model3, test_dict['X'], time_bins).detach().numpy()
            all_preds = [preds_e1, preds_e2, preds_e3]
        elif model_name == "dgp":
            all_preds = []
            for model in dgps:
                preds = torch.zeros((n_samples, time_bins.shape[0]), device=device)
                for i in range(time_bins.shape[0]):
                    preds[:,i] = model.survival(time_bins[i], test_dict['X'])
                    preds = pd.DataFrame(preds, columns=time_bins.numpy())
                    all_preds.append(preds)
        else:
            raise NotImplementedError()
        
        # Test local and global CI
        """ # TODO Confirm that global/local CI works then uncomment
        all_preds_arr = [df.to_numpy().T for df in all_preds] # convert to array
        global_ci = global_C_index(all_preds_arr, test_dict['T'].numpy(), test_dict['E'].numpy())
        local_ci = local_C_index(all_preds_arr, test_dict['T'].numpy(), test_dict['E'].numpy())
        """
        global_ci = 0
        local_ci = 0
        
        # Make evaluation for each event
        for event_id, surv_preds in enumerate(all_preds):
            n_train_samples = len(train_dict['X'])
            n_test_samples= len(test_dict['X'])
            y_train_time = train_dict['T'][:,event_id]
            y_train_event = np.array([1] * n_train_samples)
            y_test_time = test_dict['T'][:,event_id]
            y_test_event = np.array([1] * n_test_samples)
            lifelines_eval = LifelinesEvaluator(surv_preds.T, y_test_time, y_test_event,
                                                y_train_time, y_train_event)
            
            ci =  lifelines_eval.concordance()[0]
            ibs = lifelines_eval.integrated_brier_score(num_points=len(time_bins))
            mae = lifelines_eval.mae(method='Uncensored')
            d_calib = lifelines_eval.d_calibration()[0]
            
            truth_preds = torch.zeros((n_samples, time_bins.shape[0]), device=device)
            for i in range(time_bins.shape[0]):
                truth_preds[:,i] = dgps[event_id].survival(time_bins[i], test_dict['X'])
            survival_l1 = float(compute_l1_difference(truth_preds, surv_preds.to_numpy(),
                                                      n_samples, steps=time_bins))
            
            metrics = [ci, ibs, mae, survival_l1, d_calib, global_ci, local_ci]
            print(metrics)
            res_sr = pd.Series([model_name, linear, copula_name, k_tau] + metrics,
                                index=["ModelName", "Linear", "Copula", "KTau",
                                        "CI", "IBS", "MAE", "L1", "DCalib", "GlobalCI", "LocalCI"])
            model_results = pd.concat([model_results, res_sr.to_frame().T], ignore_index=True)
            model_results.to_csv(f"{cfg.RESULTS_DIR}/model_results.csv")
            