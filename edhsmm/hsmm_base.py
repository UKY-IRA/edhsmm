import numpy as np
from scipy.stats import multivariate_normal
from scipy.special import logsumexp
from sklearn import cluster
from sklearn.utils import check_array, check_random_state

from . import hsmm_core_x as core, hsmm_utils
from .hsmm_utils import log_mask_zero, iter_from_X_lengths

# Base Class for Explicit Duration HSMM
class HSMM:
    def __init__(self, n_states=2, n_durations=5, n_iter=20, tol=1e-2, rnd_state=None):
        if not n_states >= 2:
            raise ValueError("number of states (n_states) must be at least 2")
        if not n_durations >= 1:
            raise ValueError("number of durations (n_durations) must be at least 1")
        self.n_states = n_states
        self.n_durations = n_durations
        self.n_iter = n_iter
        self.tol = tol
        self.rnd_state = rnd_state

    # _init: initializes model parameters if there are none yet
    def _init(self):
        if not hasattr(self, "pi"):
            self.pi = np.full(self.n_states, 1.0 / self.n_states)
        if not hasattr(self, "tmat"):
            self.tmat = np.full((self.n_states, self.n_states), 1.0 / (self.n_states - 1))
            for i in range(self.n_states):
                self.tmat[i, i] = 0.0   # no self-transitions in EDHSMM
        self._dur_init()   # duration

    # _check: check if properties of model parameters are satisfied
    def _check(self):
        # starting probabilities
        self.pi = np.asarray(self.pi)
        if self.pi.shape != (self.n_states, ):
            raise ValueError("start probabilities (self.pi) must have shape ({},)".format(self.n_states))
        if not np.allclose(self.pi.sum(), 1.0):
            raise ValueError("start probabilities (self.pi) must add up to 1.0")
        # transition probabilities
        self.tmat = np.asarray(self.tmat)
        if self.tmat.shape != (self.n_states, self.n_states):
            raise ValueError("transition matrix (self.tmat) must have shape ({0}, {0})".format(self.n_states))
        if not np.allclose(self.tmat.sum(axis=1), 1.0):
            raise ValueError("transition matrix (self.tmat) must add up to 1.0")
        for i in range(self.n_states):
            if self.tmat[i, i] != 0.0:   # check for diagonals
                raise ValueError("transition matrix (self.tmat) must have all diagonals equal to 0.0")
        # duration probabilities
        self._dur_check()

    # _dur_init: initializes duration parameters if there are none yet
    def _dur_init(self):
        """
        arguments: (self)
        return: None
        > initialize the duration parameters
        """
        pass   # implemented in subclass
    
    # _dur_check: checks if properties of duration parameters are satisfied
    def _dur_check(self):
        """
        arguments: (self)
        return: None
        > check the duration parameters
        """
        pass   # implemented in subclass
        
    # _dur_probmat: compute the probability per state of each duration
    def _dur_probmat(self):
        """
        arguments: (self)
        return: duration probability matrix
        """
        pass   # implemented in subclass

    # _dur_mstep: perform m-step for duration parameters
    def _dur_mstep(self):
        """
        arguments: (self, new_dur)
        return: None
        > compute the duration parameters
        """
        pass   # implemented in subclass

    # _emission_logprob: compute the log-likelihood per state of each observation
    def _emission_logprob(self):
        """
        arguments: (self, X)
        return: logframe
        """
        pass   # implemented in subclass

    # _emission_pre_mstep: prepare m-step for emission parameters
    def _emission_pre_mstep(self):
        """
        arguments: (self, gamma, emission_var)
        return: None
        > process gamma and save output to emission_var
        """
        pass   # implemented in subclass

    # _emission_mstep: perform m-step for emission parameters
    def _emission_mstep(self):
        """
        arguments: (self, X, emission_var)
        return: None
        > compute the emission parameters
        """
        pass   # implemented in subclass

    # _state_sample: generate 'observation' for given state
    def _state_sample(self):
        """
        arguments: (self, state, rnd_state=None)
        return: np.ndarray of length equal to dimension of observation
        > generate sample from state
        """
        pass   # implemented in subclass

    # sample: generate random observation series
    def sample(self, n_samples=5, censoring=1, rnd_state=None):
        # self._init(None)   # see "note for programmers" in init() in GaussianHSMM
        self._check()
        # setup random state
        if rnd_state is None:
            rnd_state = self.rnd_state
        rnd_checked = check_random_state(rnd_state)
        # adapted from hmmlearn 0.2.3 (see _BaseHMM.score function)
        pi_cdf = np.cumsum(self.pi)
        tmat_cdf = np.cumsum(self.tmat, axis=1)
        dur_cdf = np.cumsum(self._dur_probmat(), axis=1)
        # for first state
        currstate = (pi_cdf > rnd_checked.rand()).argmax()   # argmax() returns only the first occurrence
        currdur = (dur_cdf[currstate] > rnd_checked.rand()).argmax() + 1
        if censoring == 0 and currdur > n_samples:
            print("SAMPLE: n_samples is too small to contain the first state duration.")
            return None
        state_sequence = [currstate] * currdur
        X = [self._state_sample(currstate, rnd_checked) for i in range(currdur)]   # generate 'observation'
        ctr_sample = currdur
        # for next state transitions
        while ctr_sample < n_samples:
            currstate = (tmat_cdf[currstate] > rnd_checked.rand()).argmax()
            currdur = (dur_cdf[currstate] > rnd_checked.rand()).argmax() + 1
            # test if now in the end of generating samples
            if ctr_sample + currdur > n_samples:
                if censoring == 0:
                    break   # if without right censoring, do not include exceeding state duration
                else:
                    currdur = n_samples - ctr_sample   # if with right censoring, cap the samples to n_samples
            state_sequence += [currstate] * currdur
            X += [self._state_sample(currstate, rnd_checked) for i in range(currdur)]   # generate 'observation'
            ctr_sample += currdur
        return ctr_sample, np.atleast_2d(X), np.array(state_sequence, dtype=int)

    # _core_u_only: container for core._u_only (for multiple observation sequences)
    def _core_u_only(self, logframe):
        n_samples = logframe.shape[0]
        u = np.empty((n_samples, self.n_states, self.n_durations))
        core._u_only(n_samples, self.n_states, self.n_durations,
                     logframe, u)
        return u

    # _core_forward: container for core._forward (for multiple observation sequences)
    def _core_forward(self, u, logdur, censoring):
        n_samples = u.shape[0]
        if censoring == 0:   # without right censoring
            eta = np.empty((n_samples, self.n_states, self.n_durations))
        else:   # with right censoring
            eta = np.empty((n_samples + self.n_durations - 1, self.n_states, self.n_durations))
        xi = np.empty((n_samples, self.n_states, self.n_states))
        core._forward(n_samples, self.n_states, self.n_durations,
                      log_mask_zero(self.pi),
                      log_mask_zero(self.tmat),
                      logdur, censoring, eta, u, xi)
        return eta, xi

    # _core_backward: container for core._backward (for multiple observation sequences)
    def _core_backward(self, u, logdur, censoring):
        n_samples = u.shape[0]
        beta = np.empty((n_samples, self.n_states))
        betastar = np.empty((n_samples, self.n_states))
        core._backward(n_samples, self.n_states, self.n_durations,
                       log_mask_zero(self.pi),
                       log_mask_zero(self.tmat),
                       logdur, censoring, beta, u, betastar)
        return beta, betastar
    
    # _core_smoothed: container for core._smoothed (for multiple observation sequences)
    def _core_smoothed(self, beta, betastar, censoring, eta, xi):
        n_samples = beta.shape[0]
        gamma = np.empty((n_samples, self.n_states))
        core._smoothed(n_samples, self.n_states, self.n_durations,
                       beta, betastar, censoring, eta, xi, gamma)
        return gamma
    
    # _core_viterbi: container for core._viterbi (for multiple observation sequences)
    def _core_viterbi(self, u, logdur, censoring):
        n_samples = u.shape[0]
        state_sequence, log_prob = core._viterbi(n_samples, self.n_states, self.n_durations,
                                                 log_mask_zero(self.pi),
                                                 log_mask_zero(self.tmat),
                                                 logdur, censoring, u)
        return state_sequence, log_prob

    # score: log-likelihood computation from observation series
    def score(self, X, lengths=None, censoring=1):
        X = check_array(X)
        # self._init(X)
        self._check()
        logdur = log_mask_zero(self._dur_probmat())   # build logdur
        # main computations
        log_prob = 0
        for i, j in iter_from_X_lengths(X, lengths):
            logframe = self._emission_logprob(X[i:j])   # build logframe
            u = self._core_u_only(logframe)
            _, betastar = self._core_backward(u, logdur, censoring)
            gammazero = log_mask_zero(self.pi) + betastar[0]
            log_prob += logsumexp(gammazero)
        return log_prob

    # predict: hidden state & duration estimation from observation series
    def predict(self, X, lengths=None, censoring=1):
        X = check_array(X)
        # self._init(X)
        self._check()
        logdur = log_mask_zero(self._dur_probmat())   # build logdur
        # main computations
        log_prob = 0
        state_sequence = np.empty(X.shape[0], dtype=int)   # total n_samples = X.shape[0]
        for i, j in iter_from_X_lengths(X, lengths):
            logframe = self._emission_logprob(X[i:j])   # build logframe
            u = self._core_u_only(logframe)
            iter_state_sequence, iter_log_prob = self._core_viterbi(u, logdur, censoring)
            log_prob += iter_log_prob
            state_sequence[i:j] = iter_state_sequence
        return state_sequence, log_prob

    # fit: parameter estimation from observation series
    def fit(self, X, lengths=None, censoring=1):
        X = check_array(X)
        self._init(X)
        self._check()
        # main computations
        for itera in range(self.n_iter):
            score = 0
            pi_num = np.full(self.n_states, -np.inf)
            tmat_num = dur_num = -np.inf
            emission_var = [None]   # see "note for programmers" in _emission_pre_mstep() in GaussianHSMM
            logdur = log_mask_zero(self._dur_probmat())   # build logdur
            for i, j in iter_from_X_lengths(X, lengths):
                logframe = self._emission_logprob(X[i:j])   # build logframe
                u = self._core_u_only(logframe)
                eta, xi = self._core_forward(u, logdur, censoring)
                beta, betastar = self._core_backward(u, logdur, censoring)
                gamma = self._core_smoothed(beta, betastar, censoring, eta, xi)
                score += logsumexp(gamma[0, :])   # this is the output of 'score' function
                # preparation for reestimation / M-step
                # this will make fit() slower than the previous version :(
                xi = np.resize(xi, (j - i + 1, self.n_states, self.n_states))
                eta = np.resize(eta, (j - i + 1, self.n_states, self.n_durations))
                xi[j - i] = tmat_num
                eta[j - i] = dur_num
                pi_num = logsumexp([pi_num, gamma[0]], axis=0)
                tmat_num = logsumexp(xi, axis=0)
                dur_num = logsumexp(eta, axis=0)
                self._emission_pre_mstep(gamma, emission_var)
            # check for loop break
            if itera > 0 and (score - old_score) < self.tol:
                print("FIT: converged at {}th loop.".format(itera + 1))
                break
            else:
                old_score = score
            # reestimation / M-step
            self.pi = np.exp(pi_num - logsumexp(pi_num))
            self.tmat = np.exp(tmat_num - logsumexp(tmat_num, axis=1)[None].T)
            new_dur = np.exp(dur_num - logsumexp(dur_num, axis=1)[None].T)
            self._dur_mstep(new_dur)   # new durations
            self._emission_mstep(X, emission_var[0])   # new emissions
            print("FIT: reestimation complete for {}th loop.".format(itera + 1))

# Sample Subclass: Explicit Duration HSMM with Gaussian Emissions
class GaussianHSMM(HSMM):
    def __init__(self, n_states=2, n_durations=5, n_iter=20, tol=1e-2, rnd_state=None):
        super().__init__(n_states, n_durations, n_iter, tol, rnd_state)

    def _init(self, X):
        super()._init()
        # note for programmers: for every attribute that needs X in score()/predict()/fit(),
        # there must be a condition 'if X is None' because sample() doesn't need an X, but
        # default attribute values must be initiated for sample() to proceed.
        if True:   # always change self.n_dim
            if X is None:   # default for sample()
                self.n_dim = 1
            else:
                self.n_dim = X.shape[1]
        if not hasattr(self, "mean"):
            if X is None:   # default for sample()
                # self.mean = [[0.], [1.], [2.], ...]
                self.mean = np.arange(0., self.n_states)[:, None]
            else:
                kmeans = cluster.KMeans(n_clusters=self.n_states, random_state=self.rnd_state)
                kmeans.fit(X)
                self.mean = kmeans.cluster_centers_
        if not hasattr(self, "covmat"):
            if X is None:   # default for sample()
                self.covmat = np.repeat(np.identity(self.n_dim)[None], self.n_states, axis=0)
            else:
                # TODO: initial covariance matrices must be computed from X
                self.covmat = np.repeat(np.identity(self.n_dim)[None], self.n_states, axis=0)

    def _check(self):
        super()._check()
        # means
        self.mean = np.asarray(self.mean)
        if self.mean.shape != (self.n_states, self.n_dim):
            raise ValueError("means (self.mean) must have shape ({}, {})"
                             .format(self.n_states, self.n_dim))
        # covariance matrices
        self.covmat = np.asarray(self.covmat)
        if self.covmat.shape != (self.n_states, self.n_dim, self.n_dim):
            raise ValueError("covariance matrices (self.covmat) must have shape ({0}, {1}, {1})"
                             .format(self.n_states, self.n_dim))

    def _dur_init(self):
        # non-parametric duration
        if not hasattr(self, "dur"):
            self.dur = np.full((self.n_states, self.n_durations), 1.0 / self.n_durations)

    def _dur_check(self):
        self.dur = np.asarray(self.dur)
        if self.dur.shape != (self.n_states, self.n_durations):
            raise ValueError("duration probabilities (self.dur) must have shape ({}, {})"
                             .format(self.n_states, self.n_durations))
        if not np.allclose(self.dur.sum(axis=1), 1.0):
            raise ValueError("duration probabilities (self.dur) must add up to 1.0")

    def _dur_probmat(self):
        # non-parametric duration
        return self.dur

    def _dur_mstep(self, new_dur):
        # non-parametric duration
        self.dur = new_dur
        
    def _emission_logprob(self, X):
        # abort EM loop if any covariance matrix is not symmetric, positive-definite.
        # adapted from hmmlearn 0.2.3 (see _utils._validate_covars function)
        for n, cv in enumerate(self.covmat):
            if (not np.allclose(cv, cv.T) or np.any(np.linalg.eigvalsh(cv) <= 0)):
                raise ValueError("component {} of covariance matrix is not symmetric, positive-definite."
                                 .format(n))
                # https://www.youtube.com/watch?v=tWoFaPwbzqE&t=1694s
        n_samples = X.shape[0]
        logframe = np.empty((n_samples, self.n_states))
        for i in range(self.n_states):
            multigauss = multivariate_normal(self.mean[i], self.covmat[i])
            for j in range(n_samples):
                logframe[j, i] = log_mask_zero(multigauss.pdf(X[j]))
        return logframe
    
    def _emission_pre_mstep(self, gamma, emission_var):
        # note for programmers: refer to "emission_var" as emission_var[0] here. Maybe this
        # is unidiomatic, but this is done to force pass-by-reference to the np.ndarray.
        # note #2: The "emssion_var" here is the cumulative concatenation of the gammas of each
        # observation sequence, so most likely you wouldn't modify this for your own subclass.
        if emission_var[0] is None:   # initial
            emission_var[0] = gamma
        else:
            old_emitlength = emission_var[0].shape[0]
            emission_var[0] = np.resize(emission_var[0], (old_emitlength + gamma.shape[0], self.n_states))
            emission_var[0][old_emitlength:] = gamma

    def _emission_mstep(self, X, emission_var):
        # note for programmers: now refer to "emission_var" as it is, here.
        denominator = logsumexp(emission_var, axis=0)
        weight_normalized = np.exp(emission_var - denominator)[None].T
        # compute means (from definition; weighted)
        self.mean = (weight_normalized * X).sum(1)
        # compute covariance matrices (from definition; weighted)
        dist = X - self.mean[:, None]
        self.covmat = ((dist * weight_normalized)[:, :, :, None] * dist[:, :, None]).sum(1)
        
    def _state_sample(self, state, rnd_state=None):
        rnd_checked = check_random_state(rnd_state)
        return rnd_checked.multivariate_normal(self.mean[state], self.covmat[state])
