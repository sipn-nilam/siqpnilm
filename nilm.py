# 
# DESCRIPTION
# Copyright (C) Weicong Kong 2016. All Right Reserved
#

import pandas as pd
import numpy as np
import collections
from gurobipy import *


class SIQP(object):

    aggregate = None
    HMMs = collections.OrderedDict()
    step_thr = 0

    timestamps = None
    n_appliances = -1
    n_segments = -1
    sol_time = -1
    max_problem_size = 3600  # 3600 time steps, considering the highest sampling frequency is 1Hz in our setting
    result = None  # state label sequence of each time step
    estimate = None

    def __init__(self, aggregate, hmms, step_thr):
        self.timestamps = aggregate.index
        aggregate.index = range(len(aggregate))
        self.aggregate = aggregate
        self.HMMs = hmms
        self.step_thr = step_thr

        self.n_appliances = len(hmms)
        self.result = pd.DataFrame(columns=range(self.n_appliances))  # init a DataFrame for result
        self.estimate = pd.DataFrame(columns=range(self.n_appliances))

    def solve(self):

        T = len(self.aggregate)
        print('Prepare to solve NILM, problem size is %s' % T)
        if T > self.max_problem_size:
            pass
        segments = self.segment_aggregate()

        results_list = list()
        segment_result = list()
        estimate_list = list()

        # solve segment by segment
        for s in range(len(segments)):
            print('Performing SIQP, solving segment No. %s' % s)
            level = segments[s].mean()
            dur = len(segments[s])

            # probability of all appliance states for lasting
            probs = list()

            for key, hmm in self.HMMs.iteritems():
                trans_mat = hmm.trans_mat
                state_dim = hmm.K
                prob = np.zeros(state_dim)
                for k in range(state_dim):
                    if k == 0:
                        prob[k] = 1  # Assuming an appliance can stay at OFF (the state=0) for arbitrarily long
                    else:
                        prob[k] = np.power(trans_mat[k, k], dur)
                probs.append(prob)

            # build integer programming model via Gurobi interface
            if s == 0:
                result, cal_level = self.solve_first_segment(level, dur)
                segment_result.append(result)
                estimate = np.array([hmm.obs_distns['mu'][result[n, 0]] for n, hmm in enumerate(self.HMMs.values())])
                estimate_list.append(estimate)
            else:
                last_result = segment_result[s - 1]
                result, cal_level = self.solver_subsequent_segment(level, dur, last_result)
                segment_result.append(result)
                estimate = np.array([hmm.obs_distns['mu'][result[n, 0]] for n, hmm in enumerate(self.HMMs.values())])
                estimate_list.append(estimate)

            # record the result
            for idx in segments[s].index:
                self.result.loc[idx, :] = result.T
                self.estimate.loc[idx, :] = estimate.T

        return

    def segment_aggregate(self):
        delta = self.aggregate.diff().abs()  # aggregate should be a pd.DataFrame or pd.Series
        change_point_indices = delta[delta > self.step_thr].index
        change_point_indices = change_point_indices.insert(0, delta.head(1).index.values)  # add the first index
        segments = list()
        for iloc, index in enumerate(change_point_indices):
            if iloc == len(change_point_indices) - 1:
                next_index = self.aggregate.index[-1] + 1
            else:
                next_index = change_point_indices[iloc + 1]
            segments.append(self.aggregate[index:next_index])  # next index not inclusive

        return segments

    def solve_first_segment(self, level, seg_dur):
        # create new Gurobi model
        model = Model('siqp')

        x_list = list()
        constraint_list = list()
        # adding variables
        n = 0
        for key, hmm in self.HMMs.iteritems():
            x = np.array([model.addVar(vtype=GRB.BINARY, name='x_%s_%s' % (n, k)) for k in range(hmm.K)])
            x = x.reshape((hmm.K, 1))
            x_list.append(x)

            # integrate new variables
            model.update()
            model.addConstr(x.sum() == 1, 'c_%s' % n)
            n += 1

        obj = 0
        agg_mu = 0
        model.update()
        n = 0
        for key, hmm in self.HMMs.iteritems():
            mus = hmm.obs_distns['mu'].values.astype(float).reshape((1, hmm.K))
            trans_mat = hmm.trans_mat
            self_trans = np.diag(trans_mat).reshape((1, hmm.K))
            agg_mu += np.dot(mus, x_list[n])[0, 0]  # should return the Expr object instead of ndarray
            obj += np.dot(np.log(np.power(self_trans, seg_dur)), x_list[n])[0, 0]
            n += 1

        # TODO: try to develop dynamic sigma, so that level is low, sigma is lower
        obj += lognormpdf(level, agg_mu, 2)  # set a reasonable overall sigma

        model.setObjective(obj, GRB.MAXIMIZE)
        model.optimize()

        # populate optimisation result
        result = np.zeros((self.n_appliances, 1))
        cal_level = 0
        if model.Status == GRB.OPTIMAL:
            n = 0
            for key, hmm in self.HMMs.iteritems():
                x_values = value(x_list[n])
                result[n] = np.argmax(x_values)
                mus = hmm.obs_distns['mu'].values.astype(float).reshape((1, hmm.K))
                cal_level += np.dot(mus, x_values)
                n += 1
        else:
            print('Gurobi Solving Fails')

        return result.astype(int), cal_level

    def solver_subsequent_segment(self, level, seg_dur, last_result):
        # create new Gurobi model
        model = Model('siqp')

        x_list = list()
        constraint_list = list()
        # adding variables
        n = 0
        for key, hmm in self.HMMs.iteritems():

            # define variables
            x = np.array([model.addVar(vtype=GRB.BINARY, name='x_%s_%s' % (n, k)) for k in range(hmm.K)])
            x = x.reshape((hmm.K, 1))
            x_list.append(x)

            # integrate new variables
            model.update()
            model.addConstr(x.sum() == 1, 'c_%s' % n)
            n += 1

        obj = 0
        agg_mu = 0
        model.update()
        n = 0
        for key, hmm in self.HMMs.iteritems():
            mus = hmm.obs_distns['mu'].values.astype(float).reshape((1, hmm.K))
            trans_mat = hmm.trans_mat
            self_trans = np.diag(trans_mat).reshape((1, hmm.K))
            agg_mu += np.dot(mus, x_list[n])[0, 0]  # should return the Expr object instead of ndarray
            obj += np.dot(np.log(np.power(self_trans, seg_dur)), x_list[n])[0, 0]
            trans = trans_mat[last_result[n], :]
            obj += np.dot(np.log(trans), x_list[n])[0, 0]
            n += 1
        obj += lognormpdf(level, agg_mu, 2)  # set a reasonable overall sigma

        model.setObjective(obj, GRB.MAXIMIZE)
        model.optimize()

        # populate optimisation result
        result = np.zeros((self.n_appliances, 1))
        cal_level = 0
        if model.Status == GRB.OPTIMAL:
            n = 0
            for key, hmm in self.HMMs.iteritems():
                x_values = value(x_list[n])
                result[n] = np.argmax(x_values)
                mus = hmm.obs_distns['mu'].values.astype(float).reshape((1, hmm.K))
                cal_level += np.dot(mus, x_values)
                n += 1
        else:
            print('Gurobi Solving Fails')

        return result.astype(int), cal_level


def lognormpdf(x, mu, sigma):
    log_prob = - np.log(sigma * np.sqrt(2.0 * np.pi)) - (x - mu) * (x - mu) / (2.0 * sigma ** 2.0)
    return log_prob


def value(grb_vars):
    values = np.array([var.X for var in grb_vars.flatten()]).astype(float).reshape(grb_vars.shape)
    return values







