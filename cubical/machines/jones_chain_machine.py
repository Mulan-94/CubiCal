# CubiCal: a radio interferometric calibration suite
# (c) 2017 Rhodes University & Jonathan S. Kenyon
# http://github.com/ratt-ru/CubiCal
# This code is distributed under the terms of GPLv2, see LICENSE.md for details
from cubical.machines.abstract_machine import MasterMachine
from cubical.machines.complex_2x2_machine import Complex2x2Gains
import numpy as np
import cubical.kernels.cyfull_complex as cyfull
import cubical.kernels.cychain as cychain

from cubical.tools import logger
log = logger.getLogger("jones_chain")

class JonesChain(MasterMachine):
    """
    This class implements a gain machine for an arbitrary chain of Jones matrices. Most of its
    functionality is consistent with a complex 2x2 solver - many of its methods mimic those of the 
    underlying complex 2x2 machines.
    """

    def __init__(self, label, data_arr, ndir, nmod, times, frequencies, jones_options):
        """
        Initialises a chain of complex 2x2 gain machines.
        
        Args:
            label (str):
                Label identifying the Jones term.
            data_arr (np.ndarray): 
                Shape (n_mod, n_tim, n_fre, n_ant, n_ant, n_cor, n_cor) array containing observed 
                visibilities. 
            ndir (int):
                Number of directions.
            nmod (nmod):
                Number of models.
            times (np.ndarray):
                Times for the data being processed.
            frequencies (np.ndarray):
                Frequencies for the data being processsed.
            jones_options (dict): 
                Dictionary of options pertaining to the chain. 
        """
        
        MasterMachine.__init__(self, label, data_arr, ndir, nmod, times, frequencies, jones_options)

        self.n_dir, self.n_mod = ndir, nmod
        _, self.n_tim, self.n_fre, self.n_ant, self.n_ant, self.n_cor, self.n_cor = data_arr.shape

        # This instantiates the number of complex 2x2 elements in our chain. Each element is a 
        # gain machine in its own right - the purpose of this machine is to manage these machines
        # and do the relevant fiddling between parameter updates. When combining DD terms with
        # DI terms, we need to be initialise the DI terms using only one direction - we do this with 
        # slicing rather than summation as it is slightly faster. 

        self.jones_terms = []
        for term_opts in jones_options['chain']:
            self.jones_terms.append(Complex2x2Gains(term_opts["label"], data_arr, 
                                    ndir if term_opts["dd-term"] else 1,
                                    nmod, times, frequencies, term_opts))

        self.n_terms = len(self.jones_terms)
        # make list of number of iterations per solvable term
        # If not specified, just use the maxiter setting of each term
        # note that this list is updated as we converge, so make a copy
        self.term_iters = list(jones_options['sol']['term-iters']) or [term.maxiter for term in self.jones_terms if term.solvable]
        self.solvable = bool(self.term_iters)

        # setup first solvable term in chain
        self.active_index = -1
        self._next_chain_term()

        cached_array_shape = [self.n_dir, self.n_mod, self.n_tim, self.n_fre, 
                              self.n_ant, self.n_ant, self.n_cor, self.n_cor]
        self.cached_model_arr = np.empty(cached_array_shape, dtype=data_arr.dtype)
        self.cached_resid_arr = np.empty(cached_array_shape, dtype=data_arr.dtype)

    def export_solutions(self):
        """ Saves the solutions to a dict of {label: solutions,grids} items. """

        soldict = {}
        # prefix jones label to solution name
        for term in self.jones_terms:
            for label, sol in term.export_solutions().iteritems():
                soldict["{}:{}".format(term.jones_label, label)] = sol
        soldict['prefixed'] = True

        return soldict

    def importable_solutions(self):
        """ Returns a dictionary of importable solutions for the chain. """

        soldict = {}
        for term in self.jones_terms:
            soldict.update(term.importable_solutions())

        return soldict

    def import_solutions(self, soldict):
        """
        Loads solutions from a dict. This should not be called -- _load_solutions()
        below should rather call import_solutions() on all the chain terms.
        """
        raise RuntimeError("This method cannot be called on a Jones chain. This is a bug.")

    def _load_solutions(self, init_sols):
        """
        Helper method invoked by Factory.create_machine() to import existing solutions into machine.
        
        In the case of a chain, we invoke this method on every member.
        """
        for term in self.jones_terms:
            term._load_solutions(init_sols)

    def compute_js(self, obser_arr, model_arr):
        """
        This function computes the (J\ :sup:`H`\J)\ :sup:`-1` and J\ :sup:`H`\R terms of the GN/LM 
        method. This method is more complicated than a more conventional gain machine. The use of
        a chain means there are additional terms which need to be considered when computing the 
        parameter updates.

        Args:
            obser_arr (np.ndarray): 
                Shape (n_mod, n_tim, n_fre, n_ant, n_ant, n_cor, n_cor) array containing the 
                observed visibilities.
            model_arr (np.ndrray): 
                Shape (n_dir, n_mod, n_tim, n_fre, n_ant, n_ant, n_cor, n_cor) array containing the 
                model visibilities.

        Returns:
            3-element tuple
                
                - J\ :sup:`H`\R (np.ndarray)
                - (J\ :sup:`H`\J)\ :sup:`-1` (np.ndarray)
                - Count of flags raised (int)     
        """     

        n_dir, n_tint, n_fint, n_ant, n_cor, n_cor = self.gains.shape

        if self.last_active_index!=self.active_index or self.iters==1:
        
            self.cached_model_arr = model_arr.copy()

            for ind in xrange(self.n_terms - 1, self.active_index, -1):
                term = self.jones_terms[ind]
                term.apply_gains(self.cached_model_arr)

            if not self.dd_term and self.n_dir>1:
                self.cached_model_arr = np.sum(self.cached_model_arr, axis=0, keepdims=True)

            self.jh = np.empty_like(self.cached_model_arr)

        self.jh[:] = self.cached_model_arr

        for ind in xrange(self.active_index, -1, -1):
            term = self.jones_terms[ind]
            cychain.cycompute_jh(self.jh, term.gains, term.t_int, term.f_int)
            
        jhr_shape = [n_dir if self.dd_term else 1, self.n_tim, self.n_fre, n_ant, n_cor, n_cor]

        jhr = np.zeros(jhr_shape, dtype=obser_arr.dtype)

        if n_dir > 1:
            resid_arr = np.empty_like(obser_arr)
            r = self.compute_residual(obser_arr, model_arr, resid_arr)
        else:
            r = obser_arr

        cyfull.cycompute_jhr(self.jh, r, jhr, 1, 1)

        for ind in xrange(0, self.active_index, 1):
            term = self.jones_terms[ind]
            g_inv = np.empty_like(term.gains)
            cyfull.cycompute_jhjinv(term.gains, g_inv, term.gflags, term.eps, term.flagbit)
            cychain.cyapply_left_inv_jones(jhr, g_inv, term.t_int, term.f_int)

        jhrint_shape = [n_dir, n_tint, n_fint, n_ant, n_cor, n_cor]
        
        jhrint = np.zeros(jhrint_shape, dtype=jhr.dtype)

        cychain.cysum_jhr_intervals(jhr, jhrint, self.t_int, self.f_int)

        jhj = np.zeros(jhrint_shape, dtype=obser_arr.dtype)

        cyfull.cycompute_jhj(self.jh, jhj, self.t_int, self.f_int)

        jhjinv = np.empty(jhrint_shape, dtype=obser_arr.dtype)

        flag_count = cyfull.cycompute_jhjinv(jhj, jhjinv, self.gflags, self.eps, self.flagbit)

        return jhrint, jhjinv, flag_count

    def compute_update(self, model_arr, obser_arr):
        """
        This function computes the update step of the GN/LM method. This is equivalent to the 
        complete (J\ :sup:`H`\J)\ :sup:`-1` J\ :sup:`H`\R.

        Args:
            obser_arr (np.ndarray): 
                Shape (n_mod, n_tim, n_fre, n_ant, n_ant, n_cor, n_cor) array containing the 
                observed visibilities.
            model_arr (np.ndrray): 
                Shape (n_dir, n_mod, n_tim, n_fre, n_ant, n_ant, n_cor, n_cor) array containing the 
                model visibilities.

        Returns:
            int:
                Count of flags raised.
        """

        # if not(self.dd_term) and model_arr.shape[0]>1:
        #     jhr, jhjinv, flag_count = self.compute_js(obser_arr, np.sum(model_arr, axis=0, keepdims=True))
        # else:
        #     jhr, jhjinv, flag_count = self.compute_js(obser_arr, model_arr)

        jhr, jhjinv, flag_count = self.compute_js(obser_arr, model_arr)

        update = np.empty_like(jhr)

        cyfull.cycompute_update(jhr, jhjinv, update)

        if self.dd_term and model_arr.shape[0]>1:
            update = self.gains + update

        if self.iters % 2 == 0 or self.dd_term:
            self.gains = 0.5*(self.gains + update)
        else:
            self.gains = update

        self.restrict_solution()

        return flag_count

    def compute_residual(self, obser_arr, model_arr, resid_arr):
        """
        This function computes the residual. This is the difference between the
        observed data, and the model data with the gains applied to it.

        Args:
            obser_arr (np.ndarray): 
                Shape (n_mod, n_tim, n_fre, n_ant, n_ant, n_cor, n_cor) array containing the 
                observed visibilities.
            model_arr (np.ndrray): 
                Shape (n_dir, n_mod, n_tim, n_fre, n_ant, n_ant, n_cor, n_cor) array containing the 
                model visibilities.
            resid_arr (np.ndarray): 
                Shape (n_mod, n_tim, n_fre, n_ant, n_ant, n_cor, n_cor) array into which the 
                computed residuals should be placed.

        Returns:
            np.ndarray: 
                Array containing the result of computing D - GMG\ :sup:`H`.
        """

        self.cached_resid_arr[:] = model_arr

        for ind in xrange(self.n_terms-1, -1, -1): 
            term = self.jones_terms[ind]
            term.apply_gains(self.cached_resid_arr)

        resid_arr[:] = obser_arr

        cychain.cycompute_residual(self.cached_resid_arr, resid_arr)

        return resid_arr

    def apply_inv_gains(self, resid_vis, corr_vis=None):
        """
        Applies the inverse of the gain estimates to the observed data matrix.

        Args:
            obser_arr (np.ndarray): 
                Shape (n_mod, n_tim, n_fre, n_ant, n_ant, n_cor, n_cor) array containing the 
                observed visibilities.
            corr_vis (np.ndarray or None, optional): 
                if specified, shape (n_mod, n_tim, n_fre, n_ant, n_ant, n_cor, n_cor) array 
                into which the corrected visibilities should be placed.

        Returns:
            np.ndarray: 
                Array containing the result of G\ :sup:`-1`\DG\ :sup:`-H`.
        """

        if corr_vis is None:
            corr_vis = np.empty_like(resid_vis)

        flag_count = 0

        for ind in xrange(self.n_terms):  
            term = self.jones_terms[ind]

            if term.dd_term:
                break

            _, fc = term.apply_inv_gains(resid_vis, corr_vis)

            flag_count += fc

            resid_vis[:] = corr_vis[:]

        return corr_vis, flag_count

    def apply_gains(self, vis):
        """
        Applies the gains to an array at full time-frequency resolution. 

        Args:
            model_arr (np.ndarray):
                Shape (n_dir, n_mod, n_tim, n_fre, n_ant, n_ant, n_cor, n_cor) array containing 
                model visibilities.

        Returns:
            np.ndarray:
                Array containing the result of GMG\ :sup:`H`.
        """
        # simply go through the chain in reverse, applying each Jones term in turn
        for term in self.jones_terms[::-1]:
            term.apply_gains(vis)
        return vis

    def update_stats(self, flags, eqs_per_tf_slot):
        """
        This method computes various stats and totals based on the current state of the flags.
        These values are used for weighting the chi-squared and doing intelligent convergence
        testing.

        Args:
            flags_arr (np.ndarray):
                Shape (n_tim, n_fre, n_ant, n_ant) array containing flags.
            eqs_per_tf_slot (np.ndarray):
                Shape (n_tim, n_fre) array containing a count of equations per time-frequency slot.
        """

        if hasattr(self.active_term, 'num_valid_intervals'):
            self.active_term.update_stats(flags, eqs_per_tf_slot)
        else:
            [term.update_stats(flags, eqs_per_tf_slot) for term in self.jones_terms]
   
    def update_conv_params(self, min_delta_g):
        """
        Updates the convergence parameters of the current time-frequency chunk. 

        Args:
            min_delta_g (float):
                Threshold for the minimum change in the gains - convergence criterion.
        """

        self.active_term.update_conv_params(min_delta_g)

    def restrict_solution(self):
        """
        Restricts the solutions by, for example, selecting a reference antenna or taking only the 
        amplitude. 
        """

        self.active_term.restrict_solution()

    def flag_solutions(self):
        """ Flags gain solutions based on certain criteria, e.g. out-of-bounds, null, etc. """

        self.active_term.flag_solutions()

    def propagate_gflags(self, flags):
        """
        Propagates the flags raised by the gain machine back into the data. This is necessary as 
        the gain flags may not have the same shape as the data.

        Args:
            flags (np.ndarray):
                Shape (n_tim, n_fre, n_ant, n_ant) array containing flags. 
        """
        
        self.active_term.propagate_gflags(flags)

    def _next_chain_term(self):
        if not self.term_iters:
            return False
        while True:
            self.active_index = (self.active_index + 1) % self.n_terms
            if self.active_term.solvable:
                self.active_term.iters = 0
                self.active_term.maxiter = self.term_iters.pop(0)
                print>> log(1), "activating term {}".format(self.active_term.jones_label)
                return True
            else:
                print>> log(1), "skipping term {}: non-solvable".format(self.active_term.jones_label)

    def update_term(self):
        """
        Updates the iteration count on the relevant element of the Jones chain. It will also handle 
        updating the active Jones term. Ultimately, this should handle any complicated 
        convergence/term switching functionality.
        """

        self.last_active_index = self.active_index

        if self.active_term.has_converged:
            print>>log(1),"term {} converged ({} iters)".format(self.active_term.jones_label, self.active_term.iters)
            self._next_chain_term()

        self.iters += 1

    @property
    def gains(self):
        return self.active_term.gains

    @gains.setter
    def gains(self, value):
        self.active_term.gains = value

    @property
    def gflags(self):
        return self.active_term.gflags

    @property
    def n_cnvgd(self):
        return self.active_term.n_cnvgd

    @property
    def n_sols(self):
        return self.active_term.n_sols

    @property
    def eqs_per_interval(self):
        return self.active_term.eqs_per_interval

    @property
    def valid_intervals(self):
        return self.active_term.valid_intervals

    @property
    def n_tf_ints(self):
        return self.active_term.n_tf_ints

    @property
    def max_update(self):
        return self.active_term.max_update

    @property
    def n_flagged(self):
        return self.active_term.n_flagged

    @property
    def num_valid_intervals(self):
        return self.active_term.num_valid_intervals

    @property
    def missing_gain_fraction(self):
        return self.active_term.missing_gain_fraction

    @property
    def old_gains(self):
        return self.active_term.old_gains

    @old_gains.setter
    def old_gains(self, value):
        self.active_term.old_gains = value

    @property
    def dtype(self):
        return self.active_term.dtype

    @property
    def ftype(self):
        return self.active_term.ftype

    @property
    def active_term(self):
        return self.jones_terms[self.active_index]

    @property
    def t_int(self):
        return self.active_term.t_int

    @property
    def f_int(self):
        return self.active_term.f_int

    @property
    def eps(self):
        return self.active_term.eps

    @property
    def flagbit(self):
        return self.active_term.flagbit

    @property
    def iters(self):
        return self.active_term.iters

    @iters.setter
    def iters(self, value):
        self.active_term.iters = value

    @property
    def maxiter(self):
        return self.active_term.maxiter

    @property
    def min_quorum(self):
        return self.active_term.min_quorum

    @property
    def has_converged(self):
        # Chain has converged when term_iters is empty -- since we take off an element each time we converge a term
        return not self.solvable or \
               ( self.active_term.has_converged and not self.term_iters )
        #return np.all([term.has_converged for term in self.jones_terms])

    @property
    def has_stalled(self):
        return np.all([term.has_stalled for term in self.jones_terms])

    @has_stalled.setter   
    def has_stalled(self, value):
        self.active_term.has_stalled = value

    @property
    def update_type(self):
        return self.active_term.update_type

    @property
    def dd_term(self):
        return self.active_term.dd_term

    class Factory(MasterMachine.Factory):
        """
        Note that a ChainMachine Factory expects a list of jones options (one dict per Jones term), not a single dict.
        """
        def __init__(self, machine_cls, grid, double_precision, apply_only, global_options, jones_options):
            # manufacture dict of "Jones options" for the outer chain
            opts = dict(label="chain", solvable=not apply_only, sol=global_options['sol'], chain=jones_options)
            self.chain_options = jones_options
            MasterMachine.Factory.__init__(self, machine_cls, grid, double_precision, apply_only,
                                           global_options, opts)

        def init_solutions(self):
            for opts in self.chain_options:
                label = opts["label"]
                self._init_solutions(label, self.make_filename(opts["load-from"], label),
                                     self.solvable and opts["solvable"] and self.make_filename(opts["save-to"], label),
                                     Complex2x2Gains.exportable_solutions())
