from __future__ import print_function

import torch
import torch.utils.data
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable

from utils.distributions import log_Bernoulli, log_Normal_diag, log_Normal_standard
from utils.visual_evaluation import plot_histogram

import numpy as np

from scipy.misc import logsumexp

def xavier_init(m):
    s =  np.sqrt( 2. / (m.in_features + m.out_features) )
    m.weight.data.normal_(0, s)

class HF(nn.Module):
    def __init__(self):
        super(HF, self).__init__()

    def forward(self, v, z):
        '''
        :param v: batch_size (B) x latent_size (L)
        :param z: batch_size (B) x latent_size (L)
        :return: z_new = z - 2* v v_T / norm(v,2) * z
        '''
        # v * v_T
        vvT = torch.bmm( v.unsqueeze(2), v.unsqueeze(1) )  # v * v_T : batch_dot( B x L x 1 * B x 1 x L ) = B x L x L
        # v * v_T * z
        vvTz = torch.bmm( vvT, z.unsqueeze(2) ).squeeze(2) # A * z : batchdot( B x L x L * B x L x 1 ).squeeze(2) = (B x L x 1).squeeze(2) = B x L
        # calculate norm ||v||^2
        norm_sq = torch.sum( v * v, 1 ) # calculate norm-2 for each row : B x 1
        norm_sq = norm_sq.expand( norm_sq.size(0), v.size(1) ) # expand sizes : B x L
        # calculate new z
        z_new = z - 2 * vvTz / norm_sq # z - 2 * v * v_T  * z / norm2(v)
        return z_new

class Gate(nn.Module):
    def __init__(self):
        super(Gate, self).__init__()

    def forward(self, h, g):
        return h * g

#=======================================================================================================================
class VAE(nn.Module):
    def __init__(self, args):
        super(VAE, self).__init__()

        self.args = args

        # encoder: q(z | x)
        self.q_z_layers_pre = nn.ModuleList()
        self.q_z_layers_gate = nn.ModuleList()

        self.q_z_layers_pre.append( nn.Linear(np.prod(self.args.input_size), 300) )
        self.q_z_layers_gate.append( nn.Linear(np.prod(self.args.input_size), 300) )

        self.q_z_layers_pre.append( nn.Linear(300, 300) )
        self.q_z_layers_gate.append( nn.Linear(300, 300) )

        self.q_z_mean = nn.Linear(300, self.args.z1_size)
        self.q_z_logvar = nn.Linear(300, self.args.z1_size)

        # decoder: p(x | z)
        self.p_x_layers_pre = nn.ModuleList()
        self.p_x_layers_gate = nn.ModuleList()

        self.p_x_layers_pre.append( nn.Linear(self.args.z1_size, 300) )
        self.p_x_layers_gate.append( nn.Linear(self.args.z1_size, 300) )

        self.p_x_layers_pre.append( nn.Linear(300, 300) )
        self.p_x_layers_gate.append( nn.Linear(300, 300) )

        self.p_x_mean = nn.Linear(300, np.prod(self.args.input_size))

        # Householder flow
        self.v_layers = nn.ModuleList()
        # T >0
        if self.args.number_of_flows > 0:
            # T = 1
            self.v_layers.append(nn.Linear(300, self.args.z1_size))
            # T > 1
            for i in range(1, self.args.number_of_flows):
                self.v_layers.append(nn.Linear(self.args.z1_size, self.args.z1_size))

        self.sigmoid = nn.Sigmoid()
        self.Gate = Gate()
        self.HF = HF()

        # Xavier initialization (normal)
        for m in self.modules():
            if isinstance(m, nn.Linear):
                xavier_init(m)

    # AUXILIARY METHODS
    def reparameterize(self, mu, logvar):
        std = logvar.mul(0.5).exp_()
        if self.args.cuda:
            eps = torch.cuda.FloatTensor(std.size()).normal_()
        else:
            eps = torch.FloatTensor(std.size()).normal_()
        eps = Variable(eps)
        return eps.mul(std).add_(mu)

    def calculate_likelihood(self, X, dir, mode='test', S=5000):
        # set auxiliary variables for number of training and test sets
        N_test = X.size(0)

        # init list
        likelihood_test = []

        MB = 500

        if S <= MB:
            R = 1
        else:
            R = S / MB
            S = MB

        for j in range(N_test):
            if j % 100 == 0:
                print('{:.2f}%'.format(j / (1. * N_test) * 100))
            # Take x*
            x_single = X[j].unsqueeze(0)

            a = []
            for r in range(0, R):
                # Repeat it for all training points
                x = x_single.expand(S, x_single.size(1))

                # pass through VAE
                x_mean, x_logvar, z_0, z_T, z_q_mean, z_q_logvar = self.forward(x)

                # RE
                RE = log_Bernoulli(x, x_mean, dim=1)

                # KL
                log_p_z = log_Normal_standard(z_T, dim=1)
                log_q_z = log_Normal_diag(z_0, z_q_mean, z_q_logvar, dim=1)
                KL = -(log_p_z - log_q_z)

                a_tmp = (RE - KL)

                a.append( a_tmp.cpu().data.numpy() )

            # calculate max
            a = np.asarray(a)
            a = np.reshape(a, (a.shape[0] * a.shape[1], 1))
            likelihood_x = logsumexp( a )
            likelihood_test.append(likelihood_x - np.log(len(a)))

        likelihood_test = np.array(likelihood_test)

        plot_histogram(-likelihood_test, dir, mode)

        return -np.mean(likelihood_test)

    def calculate_lower_bound(self, X_full):
        # CALCULATE LOWER BOUND:
        lower_bound = 0.
        RE_all = 0.
        KL_all = 0.

        MB = 500

        for i in range(X_full.size(0) / MB):
            x = X_full[i * MB: (i + 1) * MB].view(-1, np.prod(self.args.input_size))

            # pass through VAE
            x_mean, x_logvar, z_0, z_T, z_q_mean, z_q_logvar = self.forward(x)

            # RE
            RE = log_Bernoulli( x, x_mean )

            # KL
            log_p_z = log_Normal_standard( z_T, dim=1)
            log_q_z = log_Normal_diag( z_0, z_q_mean, z_q_logvar, dim=1 )
            KL = - torch.sum( log_p_z - log_q_z )

            RE_all += RE.cpu().data[0]
            KL_all += KL.cpu().data[0]

            # CALCULATE LOWER-BOUND: RE + KL - ln(N)
            lower_bound += (-RE + KL).cpu().data[0]

        lower_bound = lower_bound / X_full.size(0)

        return lower_bound

    # THE MODEL: VARIATIONAL POSTERIOR
    def q_z(self, x):
        h0_pre = self.q_z_layers_pre[0](x)
        h0_gate = self.sigmoid( self.q_z_layers_gate[0](x) )
        h0 = self.Gate( h0_pre, h0_gate )

        h1_pre = self.q_z_layers_pre[1](h0)
        h1_gate = self.sigmoid( self.q_z_layers_gate[1](h0) )
        h1 = self.Gate( h1_pre, h1_gate )

        z_q_mean = self.q_z_mean(h1)
        z_q_logvar = self.q_z_logvar(h1)
        return z_q_mean, z_q_logvar, h1

    # THE MODEL: HOUSEHOLDER FLOW
    def q_z_Flow(self, z, h_last):
        v = {}
        # Householder Flow:
        if self.args.number_of_flows > 0:
            v['1'] = self.v_layers[0](h_last)
            z['1'] = self.HF(v['1'], z['0'])
            for i in range(1, self.args.number_of_flows):
                v[str(i + 1)] = self.v_layers[i](v[str(i)])
                z[str(i + 1)] = self.HF(v[str(i + 1)], z[str(i)])
        return z

    # THE MODEL: GENERATIVE DISTRIBUTION
    def p_x(self, z):
        h0_pre = self.p_x_layers_pre[0](z)
        h0_gate = self.sigmoid( self.p_x_layers_gate[0](z) )
        h0 = self.Gate( h0_pre, h0_gate )

        h1_pre = self.p_x_layers_pre[1](h0)
        h1_gate = self.sigmoid( self.p_x_layers_gate[1](h0) )
        h1 = self.Gate( h1_pre, h1_gate )

        x_mean = self.sigmoid( self.p_x_mean(h1) )
        x_logvar = 0.
        return x_mean, x_logvar

    # THE MODEL: FORWARD PASS
    def forward(self, x):
        z = {}
        # z ~ q(z | x)
        z_q_mean, z_q_logvar, h_last = self.q_z(x)
        z['0'] = self.reparameterize(z_q_mean, z_q_logvar)
        # Householder Flow:
        z = self.q_z_Flow(z, h_last)

        # x_mean = p(x|z)
        x_mean, x_logvar = self.p_x(z[str(self.args.number_of_flows)] )

        return x_mean, x_logvar, z['0'], z[str(self.args.number_of_flows)], z_q_mean, z_q_logvar
