import os
import warnings
from copy import deepcopy
from inspect import getfullargspec
from typing import Mapping, Sequence
import numpy as np
from tqdm import tqdm

from cobaya.model import Model
from cobaya.collection import SampleCollection

from gpry import mpi
from gpry.proposal import InitialPointProposer, ReferenceProposer, PriorProposer, \
    UniformProposer, MeanCovProposer
from gpry.gpr import GaussianProcessRegressor
from gpry.gp_acquisition import GenericGPAcquisition
import gpry.gp_acquisition as gprygpacqs
import gpry.acquisition_functions as gpryacqfuncs
from gpry.svm import SVM
from gpry.preprocessing import Normalize_bounds, Normalize_y
import gpry.convergence as gpryconv
from gpry.progress import Progress, Timer, TimerCounter
from gpry.io import create_path, check_checkpoint, read_checkpoint, save_checkpoint
from gpry.mc import mc_sample_from_gp, process_gdsamples
import gpry.plots as gpplt
from gpry.tools import create_cobaya_model, get_Xnumber, check_candidates, is_in_bounds

global _plots_path
_plots_path = "images"


class Runner():
    r"""
    Class that takes care of constructing the Bayesian quadrature/likelihood
    characterization loop. After initialisation, the algorithm can be launched with
    :func:`Runner.run`, and, optionally after that, an MC process can be launched on the
    surrogate model with :func:`Runner.generate_mc_sample`.

    Parameters
    ----------
    model : callable or Cobaya `model object <https://cobaya.readthedocs.io/en/latest/cosmo_model.html>`_
        Likelihood function (returning log-likelihood; requires additional argument
        ``bounds``) or Cobaya Model instance (which contains all information about the
        parameters in the likelihood and their priors as well as the likelihood itself).
        It must not be specified if 'resuming' from a checkpoint (see ``load_checkpoint``
        below).

    bounds: List of [min, max], or Dict {name: [min, max],...}
        List or dictionary of parameter bounds. If it is a dictionary, the keys need to
        correspond to the argument names of the ``likelihood`` function, and the values
        can be either bounds specified as ``[min, max]``, or bounds and labels, as
        ``{"prior": [min, max], "latex": [label]}``. It does not need to be defined (will
        be ignored) if a Cobaya ``Model`` instance is passed as ``model``.

    gpr : GaussianProcessRegressor, str, dict, optional (default="RBF")
        The GP used for interpolating the posterior. If None or "RBF" is given
        a GP with a constant kernel multiplied with an anisotropic RBF kernel
        and dynamic bounds is generated. The same kernel with a Matern 3/2
        kernel instead of a RBF is generated if "Matern" is passed. This might
        be useful if the posterior is not very smooth. Otherwise a custom GP regressor can
        be defined as a dict containing the arguments of ``GaussianProcessRegressor``, or
        passing an already initialized instance.

    gp_acquisition : GenericGPAcquisition, optional (default="LogExp")
        The acquisition object. If None is given the BatchOptimizer with a LogExp
        acquisition function is used (with the :math:`\zeta` value chosen automatically
        depending on the dimensionality of the prior) and the GP's X-values are
        preprocessed to be in the uniform hypercube before optimizing the
        acquistion function. It can also be passed an initialized instance, or a dict with
        arguments with which to initialize one.

    initial_proposer : InitialPointProposer, str, dict, optional (default="reference")
        Proposer used for drawing the initial training samples before running the
        Bayesian optimisation loop. As standard the samples are drawn from the model
        reference (prior if no reference is specified). Alternative options which can be
        passed as strings are ``"prior", "uniform"``. The ``"reference"`` proposer
        defaults to the prior if no reference distribution is provided. If defined as a
        dict with the proposer name as single key, the values will be passed as kwargs to
        the proposer.

    convergence_criterion : ConvergenceCriterion, str, dict, False, optional (default=None)
        The convergence criterion. If None is given the default criterion is used:
        CorrectCounter for BatchOptimizer with adaptive thresholds, and a combination of
        a less stringent CorrectCounter and a GaussianKL for NORA. Can be specified as a
        dict to initialize one or more ConvergenceCriterion classes with some arguments,
        or directly as an instance or class name of some ConvergenceCriterion. If False,
        no convergence criterion is used, and the process runs until the budget is
        exhausted.

    options : dict, optional (default=None)
        A dict containing all options regarding the bayesian optimization loop.
        The available options are:

            * n_initial : Number of finite initial truth evaluations before starting the
              BO loop (default: 3 * number of dimensions)
            * max_initial : Maximum number of truth evaluations at initialization. If it
              is reached before `n_initial` finite points have been found, the run will
              fail. To avoid that, try decreasing the volume of your prior (default:
              30 * (number of dimensions)**1.5).
            * max_total : Maximum number of attempted sampling points before the run
              stops. This is useful if you e.g. want to restrict the maximum computation
              resources (default: 70 * (number of dimensions)**1.5 or max_initial,
              whichever is largest).
            * max_finite : Maximum number of sampling points accepted into the GP training
              set before the run stops. This might be useful if you use the DontConverge
              convergence criterion, specifying exactly how many points you want to have
              in your GP. If you set this limit by hand and find that it is easily
              saturated, try decreasing the volume of your prior (default: max_total).
            * n_points_per_acq : Number of points which are aquired with Kriging believer
              for every acquisition step. It will be adjusted within a 20% tolerance to
              match a multiple of the number of parallel processes (default: number of
              dimensions).
            * fit_full_every : Number of iterations between full GP hyperparameters fits,
              including several restarts of the optimiser. Pass 'np.inf' or a large number
              to never refit with restarts (default : 2 * sqrt(number of dimensions)).
            * fit_simple_every : Similar to ``fit_full_every``, but with a single
              optimiser run from the last optimum hyperparameters. Overridden by
              ``fit_full_every`` where it matches its periodicity. Pass np.inf or a large
              number to never refit from last optimum (default : 1, i.e. every iteration).

    callback : callable, optional (default=None)
        Function run each iteration after adapting the recently acquired points and
        the computation of the convergence criterion. This function should take the
        runner as argument: ``callback(runner_instance)``.
        When running in parallel, the function is run by the main process only, unless
        ``callback_is_MPI_aware=True``.

    callback_is_MPI_aware : bool (default: False)
        If True, the callback function is called for every process simultaneously, and
        it is expected to handle parallelisation internally. If false, only the main
        process calls it.

    checkpoint : str, optional (default=None)
        Path for storing checkpointing information from which to resume in case the
        algorithm crashes. If None is given no checkpoint is saved.

    load_checkpoint: "resume" or "overwrite", must be specified if path is not None.
        Whether to resume from the checkpoint files if existing ones are found
        at the location specified by `checkpoint`.

    seed: int, optional
        Seed for the random number generator. Allows for reproducible runs.

    plots : bool, dict (default: True)
        If True, produces some progress plots. One can also pass the arguments of
        ``Runner.plot_progress`` as a dict for finer control, e.g.
        ``{"timing": True, "convergence": True, "trace": False, "slices": False,
        "format": "svg"}``.

    verbose : 1, 2, 3, optional (default: 3)
        Level of verbosity. 3 prints Infos, Warnings and Errors, 2
        Warnings and Errors, and 1 only Errors. Should be set to 2 or 3 if
        problems arise. Is passed to the GP, Acquisition and Convergence
        criterion if they are built automatically.

    Attributes
    ----------
    model : Cobaya model
        The model that was used to run the GP on (if running in parallel, needs to be
        passed for all processes).

    gpr : GaussianProcessRegressor
        This can be used to call an MCMC sampler for getting marginalized
        properties. This is the most crucial component.

    gp_acquisition : GenericGPAcquisition
        The acquisition object that was used for the active sampling procedure.

    convergence_criterion : Convergence_criterion
        The convergence criterion used for determining convergence. Depending
        on the criterion used this also contains the approximate covariance
        matrix of the posterior distribution which can be used by the MCMC
        sampler.

    options : dict
        The options dict used for the active sampling loop.

    progress : Progress
        Object containing per-iteration progress information: number of finite training
        points, number of GP evaluations, timing of different parts of the algorithm, and
        value of the convergence criterion.
    """

    def __init__(self,
                 model=None,
                 bounds=None,
                 gpr="RBF",
                 gp_acquisition="LogExp",
                 initial_proposer="reference",
                 convergence_criterion=None,
                 callback=None,
                 callback_is_MPI_aware=False,
                 options=None,
                 checkpoint=None,
                 load_checkpoint=None,
                 seed=None,
                 plots=False,
                 verbose=3,
                 ):
        self.verbose = verbose
        if model is None:
            if not (checkpoint is not None and str(load_checkpoint).lower() == "resume"):
                raise ValueError(
                    "'model' must be specified unless resuming from a checkpoint.")
        elif isinstance(model, Model):
            self.model = model
        elif callable(model):
            if bounds is None:
                raise ValueError("'bounds' need to be defined if a likelihood "
                                 "function is passed.")
            self.model = create_cobaya_model(model, bounds)
        self.checkpoint = checkpoint
        if self.checkpoint is not None:
            self.plots_path = os.path.join(self.checkpoint, _plots_path)
            if mpi.is_main_process:
                create_path(self.checkpoint, verbose=self.verbose >= 3)
                if plots:
                    create_path(self.plots_path, verbose=self.verbose >= 3)
        else:
            self.plots_path = _plots_path
            if plots and mpi.is_main_process:
                create_path(self.plots_path, verbose=self.verbose >= 3)
        self.plots = plots
        self.ensure_paths(plots=self.plots)
        self.random_state = mpi.get_random_state(seed)
        if mpi.is_main_process:
            self.options = deepcopy(options) or {}
            # Check if a checkpoint exists already and if so resume from there
            self.loaded_from_checkpoint = False
            if checkpoint is not None:
                if load_checkpoint not in ["resume", "overwrite"]:
                    raise ValueError("If a checkpoint location is specified you need to "
                                     "set 'load_checkpoint' to 'resume' or 'overwrite'.")
                if load_checkpoint == "resume":
                    self.log("Checking for checkpoint to resume from...", level=3)
                    checkpoint_files = check_checkpoint(checkpoint)
                    self.loaded_from_checkpoint = np.all(checkpoint_files)
                    if self.loaded_from_checkpoint:
                        self.read_checkpoint(model=model)
                        # Overwrite internal parameters by those loaded from checkpoint.
                        model, gpr, gp_acquisition, convergence_criterion, options = \
                            self.model, self.gpr, self.acquisition, self.convergence, \
                            self.options
                        self.log("#########################################\n"
                                 "Checkpoint found. Resuming from there...\n"
                                 "If this behaviour is unintentional either\n"
                                 "turn the checkpoint option off or rename it\n"
                                 "to a file which doesn't exist.\n"
                                 "#########################################\n", level=3)
                    else:
                        if np.any(checkpoint_files):
                            self.log("warning: Found checkpoint files but they were "
                                     "incomplete. Ignoring them...", level=2)
            # Check model
            if not isinstance(model, Model) and not callable(model):
                if load_checkpoint == "resume":
                    raise ValueError(f"Resuming from checkpoint {checkpoint} failed. "
                                     "In this case, a 'model' needs to be specified.")
                else:
                    raise TypeError("'model' needs to be a likelihood function or a "
                                    f"Cobaya model. got {model!r}")
            try:
                self.prior_bounds = self.model.prior.bounds(
                    confidence_for_unbounded=0.99995)
            except Exception as excpt:
                raise RuntimeError("There seems to be something wrong with "
                                   f"the model instance: {excpt}") from excpt
            # Construct the main loop elements (and options):
            # GPR, GPAcquisition, InitialProposer and ConvergenceCriterion
            self._construct_gpr(gpr)
            self._construct_gp_acquisition(gp_acquisition)
            self._construct_initial_proposer(initial_proposer)
            self._construct_convergence_criterion(
                convergence_criterion,
                acq_has_mc=isinstance(self.acquisition, gprygpacqs.NORA),
            )
            self._construct_options(options)
            # Diagnosis
            self.diagnosis = options.get("diagnosis", None)
            # Callback
            self.callback = callback
            self.callback_is_MPI_aware = callback_is_MPI_aware
            # Print resume
            self.log("Initialized GPry.", level=3)
        if mpi.multiple_processes:
            for attr in ("n_initial", "max_initial", "max_total", "max_finite",
                         "n_points_per_acq", "fit_full_every", "fit_simple_every",
                         "options", "acquisition",
                         "callback_is_MPI_aware", "loaded_from_checkpoint",
                         "initial_proposer", "progress", "diagnosis",
                         "n_resamples_before_giveup", "resamples"):
                mpi.share_attr(self, attr)
            self._share_gpr()
            self._share_convergence_from_main()
            if self.callback_is_MPI_aware:
                mpi.share_attr(self, "callback")
            else:  # for check of whether to call it
                callback_func = callback
                self.callback = mpi.comm.bcast(
                    (callback is not None) if mpi.is_main_process else None)
                if mpi.is_main_process:
                    self.callback = callback_func
        # Prepare progress summary table; the table key is the iteration number
        if not self.loaded_from_checkpoint:
            self.progress = Progress()
        # Prepare logpriorvolume to subtract
        self.prior_bounds = self.model.prior.bounds(confidence_for_unbounded=0.99995)
        self.log_prior_volume = np.sum(
            np.log(self.prior_bounds[:, 1] - self.prior_bounds[:, 0])
        )
        self.current_iteration = 0
        self.has_run = False
        self.has_converged = False
        self.old_gpr, self.new_X, self.new_y, self.y_pred = None, None, None, None
        self.mean, self.cov = None, None
        self.last_mc_surr_info, self.last_mc_sampler = None, None
        self._last_mc_samples = None
        self._is_model_saved = False

    def _construct_gpr(self, gpr):
        """Constructs or passes the GPR."""
        if isinstance(gpr, GaussianProcessRegressor):
            self.gpr = gpr
        elif isinstance(gpr, (Mapping, str)):
            if isinstance(gpr, str):
                gpr = {"kernel": gpr}
            else:  # Mapping
                gpr = deepcopy(gpr)
            gpr_defaults = {
                "kernel": "RBF",
                "n_restarts_optimizer": 10 + 2 * self.d,
                "preprocessing_X": Normalize_bounds(self.prior_bounds),
                "preprocessing_y": Normalize_y(),
                "bounds": self.prior_bounds,
                "random_state": self.random_state,
                "verbose": self.verbose,
                "account_for_inf": "SVM",
                "inf_threshold": "20s"
            }
            for k, default_value in gpr_defaults.items():
                if k not in gpr:
                    gpr[k] = default_value
            gpr["n_restarts_optimizer"] = get_Xnumber(
                gpr["n_restarts_optimizer"], "d", self.d, int, "n_restarts_optimizer"
            )
            # If running with MPI, round down the #restarts of hyperparam optimizer to
            # a multiple of the MPI size (NB: #restarts includes from current best)
            if (
                    gpr["n_restarts_optimizer"] > mpi.SIZE and
                    gpr["n_restarts_optimizer"] % mpi.SIZE
            ):
                gpr["n_restarts_optimizer"] = (
                    (gpr["n_restarts_optimizer"] // mpi.SIZE) * mpi.SIZE
                )
                warnings.warn(
                    "The number of restarts of the optimizer has been rounded down to "
                    f"{gpr['n_restarts_optimizer']} to better exploit parallelization."
                )
            try:
                self.gpr = GaussianProcessRegressor(**gpr)
            except ValueError as excpt:
                raise ValueError(
                    f"Error when initializing the GP regressor: {str(excpt)}"
                ) from excpt
        else:
            raise TypeError(
                "'gpr' should be a GP regressor, a dict of arguments for the GPR, "
                "or a string specifying the kernel ('RBF' or 'Matern'). Got {gpr}"
            )

    def _construct_gp_acquisition(self, gp_acquisition):
        """Constructs or passes the GPAcquisition instance."""
        default_gq_acquisition = "BatchOptimizer"
        if isinstance(gp_acquisition, GenericGPAcquisition):
            self.acquisition = gp_acquisition
        elif isinstance(gp_acquisition, (Mapping, str, type(None))):
            if gp_acquisition is None:
                gp_acquisition = {default_gq_acquisition: {}}
            elif isinstance(gp_acquisition, str):
                gp_acquisition = {gp_acquisition: {}}
            else:  # Mapping
                gp_acquisition = deepcopy(gp_acquisition)
            # If an acq_func name was passed, use the standard batch-optimization one
            if list(gp_acquisition)[0] in gpryacqfuncs.builtin_names():
                gp_acquisition = {
                    default_gq_acquisition: {"acq_func": {list(gp_acquisition)[0]: {}}}}
            gp_acquisition_name = list(gp_acquisition)[0]
            gp_acquisition_args = gp_acquisition[gp_acquisition_name] or {}
            gp_acquisition_defaults = {
                "bounds": self.prior_bounds,
                "preprocessing_X": self.gpr.preprocessing_X,
                "random_state": self.random_state,
                "acq_func": {"LogExp": {"zeta_scaling": 0.85}},
                "verbose": self.verbose,
            }
            for k, default_value in gp_acquisition_defaults.items():
                if k not in gp_acquisition_args:
                    gp_acquisition_args[k] = default_value
            try:
                gp_acquisition_class = getattr(gprygpacqs, gp_acquisition_name)
            except AttributeError as excpt:
                raise ValueError(
                    f"Unknown GPAcquisiton class {gp_acquisition_name}. "
                    f"Available GPAcquisition classes: {gprygpacqs.builtin_names()}"
                ) from excpt
            try:
                self.acquisition = gp_acquisition_class(**gp_acquisition_args)
            except Exception as excpt:
                raise ValueError(
                    "Error when initialising the GPAcquisition object "
                    f"{gp_acquisition_name} with arguments {gp_acquisition_args}: "
                    f"{str(excpt)}"
                ) from excpt
        else:
            raise TypeError(
                "'gp_acquisition' should be a GPAcquisition object, "
                "or a dict or string specification for one of "
                f"{gprygpacqs.builtin_names()}. Got {gp_acquisition}"
            )

    def _construct_initial_proposer(self, initial_proposer):
        """Constructs or passes the initial proposer."""
        if isinstance(initial_proposer, InitialPointProposer):
            self.initial_proposer = initial_proposer
        elif isinstance(initial_proposer, (Mapping, str)):
            if isinstance(initial_proposer, str):
                initial_proposer = {initial_proposer: {}}
            else:  # Mapping
                initial_proposed = deepcopy(initial_proposer)
            initial_proposer_name = list(initial_proposer)[0]
            initial_proposer_args = initial_proposer[initial_proposer_name]
            propname_nosuffix = initial_proposer_name.lower().removesuffix("proposer")
            if propname_nosuffix == "reference":
                self.initial_proposer = ReferenceProposer(
                    self.model, **initial_proposer_args)
            elif propname_nosuffix == "prior":
                self.initial_proposer = PriorProposer(
                    self.model, **initial_proposer_args)
            elif propname_nosuffix == "uniform":
                self.initial_proposer = UniformProposer(
                    self.prior_bounds, **initial_proposer_args)
            elif propname_nosuffix == "meancov":
                self.initial_proposer = MeanCovProposer(
                    **initial_proposer_args)
            else:
                raise ValueError(
                    "Supported standard initial point proposers are "
                    f"'reference', 'prior', 'uniform'. Got {initial_proposer}")
        else:
            raise TypeError(
                "'initial_proposer' should be an InitialPointProposer instance, a "
                "dict specification, or one of 'reference', 'prior' or 'uniform'. "
                f" Got {initial_proposer}"
            )

    def _construct_convergence_criterion(self, convergence_criterion, acq_has_mc=False):
        """Constructs or passes the convergence criterion."""
        # Special case: False = DontConverge
        if convergence_criterion is False:
            self.convergence = [gpryconv.DontConverge()]
            return
        if convergence_criterion is None:
            # Use defaults
            convergence_criterion = ["CorrectCounter"]
            if acq_has_mc:
                convergence_criterion += ["GaussianKL"]
        elif isinstance(convergence_criterion, Mapping):
            # In principle, deepcopy, but keep values that are ConvergenceCriterion as is!
            convergence_criterion_copy = {}
            for k, v in convergence_criterion.items():
                if isinstance(v, gpryconv.ConvergenceCriterion):
                    convergence_criterion_copy[k] = v
                else:
                    convergence_criterion_copy[k] = deepcopy(v)
            convergence_criterion = convergence_criterion_copy
        # Make sure it is a list or a dict
        if (
                (not isinstance(convergence_criterion, Sequence) and
                 not isinstance(convergence_criterion, Mapping)) or
                isinstance(convergence_criterion, str)
        ):
            convergence_criterion = [convergence_criterion]
        self.convergence = []
        for cc in convergence_criterion:
            if isinstance(cc, gpryconv.ConvergenceCriterion):
                self.convergence.append(cc)
                continue
            if not isinstance(cc, str) and not isinstance(cc, dict):
                raise TypeError(
                    "'convergence_criterion' should be a ConvergenceCriterion instance, "
                    "or a dict or string specification for one or more of "
                    f"{gpryconv.builtin_names()}. Got {cc}"
                )
            try:
                convergence_class = getattr(gpryconv, cc)
            except AttributeError as excpt:
                raise ValueError(
                    f"Unknown convergence criterion {cc}. "
                    f"Available convergence criteria: {gpryconv.builtin_names()}"
                ) from excpt
            args = (
                convergence_criterion[cc] or {}
                if isinstance(convergence_criterion, Mapping) else {}
            )
            try:
                self.convergence.append(convergence_class(self.prior_bounds, args))
            except Exception as excpt:
                raise ValueError(
                    "Error when initialising the convergence criterion "
                    f"{cc} with arguments {args}: "
                    f"{str(excpt)}"
                ) from excpt

    def _construct_options(self, options):
        if options is None:
            options = {}
        _opt_or_default = lambda optname, default: (
            options.get(optname, default) if options.get(optname, default) is not None
            else default
        )
        _get_opt = lambda optname, default: get_Xnumber(
            _opt_or_default(optname, default), "d", self.d, dtype=int, varname=optname
        )
        self.n_initial = max(_get_opt("n_initial", 3 * self.d), 2)  # at least 2 points!
        self.max_initial = _get_opt("max_initial", 30 * self.d**1.5)
        self.max_total = _get_opt("max_total", max(self.max_initial, 70 * self.d**1.5))
        self.max_finite = _get_opt("max_finite", self.max_total)
        self.n_points_per_acq = _get_opt("n_points_per_acq", self.d)
        self.fit_full_every = max(_get_opt("fit_full_every", 2 * np.sqrt(self.d)), 1)
        self.fit_simple_every = max(_get_opt("fit_simple_every", 1), 1)
        # TODO: undocumented option (under testing):
        self.n_resamples_before_giveup = _get_opt("n_resamples_before_giveup", 2)
        self.resamples = 0
        # Sanity checks/adjustments
        for attr in ["n_initial", "max_initial", "max_finite",
                     "max_total", "n_points_per_acq",
                     "fit_full_every", "fit_simple_every",
                     ]:
            _large_value = 1000000000
            setattr(self, attr, min(_large_value, int(np.round(getattr(self, attr)))))
            if getattr(self, attr) <= 0:
                raise ValueError(f"'{attr}' must be a positive integer.")
        if self.max_initial < self.n_initial:
            raise ValueError(
                "The number of maximum initial evaluations "
                f"'max_initial={self.max_initial}' needs to be larger than or equal to "
                f"the number of initial finite evaluations 'n_initial={self.n_initial}'."
            )
        if self.max_finite < self.n_initial:
            raise ValueError(
                f"The total number of finite evaluations 'max_finite={self.max_finite}' "
                "needs to be larger than or equal to the number of initial finite "
                f"evaluations 'n_initial={self.n_initial}'."
            )
        if self.max_total < self.max_initial:
            raise ValueError(
                f"The total number of evaluations 'max_total={self.max_total}' needs to "
                "be larger than or equal to the maximum number of initial evaluations "
                f"'max_initial={self.max_initial}'."
            )
        if self.max_total < self.max_finite:
            raise ValueError(
                f"The maximum total number of evaluations 'max_total={self.max_total}' "
                "needs to be larger than or equal to the maximum number of finite ones "
                f"'max_finite={self.max_finite}'."
            )
        if self.n_points_per_acq > self.d:
            self.log(
                "Warning: The number kriging believer samples per acquisition step "
                f"'n_points_per_acq={self.n_points_per_acq}' is larger than the number "
                f"of dimensions 'd={self.d}' of the feature space. This may lead to slow "
                "convergence.",
                level=2,
            )
        # Adjust n_points_acq to num of MPI processes within 20% tolerance (always down)
        rest_n_acq = self.n_points_per_acq % mpi.SIZE
        if rest_n_acq <= 0.2 * self.n_points_per_acq and rest_n_acq != 0:
            new_n_acq = self.n_points_per_acq - rest_n_acq
            self.log(
                "Warning: The number kriging believer samples per acquisition step "
                f"'n_points_per_acq={self.n_points_per_acq}' has been rounded down to "
                f"{new_n_acq} to better exploit parallelisation.",
                level=2,
            )
            self.n_points_per_acq= new_n_acq

    @property
    def d(self):
        """Dimensionality of the problem."""
        return self.model.prior.d()

    def logp(self, X):
        """
        Wrapper for the surrogate posterior. Call with a point or a list of them.

        This is the full posterior. If the prior is uniform the likelihood function can be
        recovered by summing ``self.log_prior_volume`` to this function.

        Always returns an array.
        """
        return self.gpr.predict(np.atleast_2d(X))

    def log(self, msg, level=None):
        """
        Print a message if its verbosity level is equal or lower than the given one (or
        always if ``level=None``.
        """
        if level is None or level <= self.verbose:
            print(msg)

    def ensure_paths(self, plots=True):
        """
        Creates paths for checkpoint and plots.
        """
        if mpi.is_main_process:
            if self.checkpoint:
                create_path(self.checkpoint, verbose=self.verbose >= 3)
            if plots:
                create_path(self.plots_path, verbose=self.verbose >= 3)

    @property
    def n_total_left(self):
        """Number of truth evaluations before stopping."""
        return self.max_total - self.gpr.n_total

    @property
    def n_finite_left(self):
        """Number of truth evaluations with finite return value before stopping."""
        return self.max_finite - self.gpr.n_finite

    def banner(self, text, max_line_length=79, prefix="| ", suffix=" |",
               header="=", footer="=", level=3):
        """Creates an iteration banner."""
        default_header_footer = "="
        if header:
            if not isinstance(header, str):
                header = default_header_footer
            self.log(max_line_length * str(header), level=level)
        text = text.strip("\n")
        lines = text.split("\n")
        for line in lines:
            line = prefix + line
            left_before_suffix = max_line_length - len(line) - len(suffix)
            if left_before_suffix >= 0:
                line += " " * left_before_suffix + suffix
            self.log(line, level=level)
        if footer:
            if not isinstance(footer, str):
                footer = default_header_footer
            self.log(max_line_length * str(footer), level=level)

    def read_checkpoint(self, model=None):
        """
        Loads checkpoint files to be able to resume a run or save the results for
        further processing.

        Parameters
        ----------
        model : cobaya.model.Model, optional
            If passed, it will be used instead of the loaded one.
        """
        self.model, self.gpr, self.acquisition, self.convergence, self.options, \
            self.progress = read_checkpoint(self.checkpoint, model=model)

    def save_checkpoint(self, update_model=False):
        """
        Saves checkpoint files to be able to resume a run or save the results for
        further processing.
        """
        if mpi.is_main_process:
            to_save_model = None
            if update_model or not self._is_model_saved:
                to_save_model = self.model
                self._is_model_saved = True
            save_checkpoint(self.checkpoint, to_save_model, self.gpr, self.acquisition,
                            self.convergence, self.options, self.progress)

    def _share_gpr(self, root=0):
        """
        Shares the GPR of the main process, restoring each process' RNG.
        """
        if not mpi.multiple_processes:
            return
        mpi.share_attr(self, "gpr", root=root)
        self.gpr.set_random_state(self.random_state)

    def _share_convergence_from_main(self):
        """
        Shares the convergence criterion from the main process, aware of whether any of
        the criteria handles MPI by itself.
        """
        if not mpi.multiple_processes:
            return
        if mpi.is_main_process:
            mpi_awareness = [cc.is_MPI_aware for cc in self.convergence]
        else:
            mpi_awareness = None
        mpi_awareness = mpi.comm.bcast(mpi_awareness)
        if not mpi.is_main_process:
            self.convergence = [gpryconv.DummyMPIConvergeCriterion()] * len(mpi_awareness)
        for i, (cc, is_MPI) in enumerate(zip(self.convergence, mpi_awareness)):
            if is_MPI:
                self.convergence[i] = mpi.comm.bcast(cc)

    def run(self):
        r"""
        Runs the acquisition-training-convergence loop until either convergence or
        a stopping condition is reached.
        """
        if self.has_run:
            self.log("The GP fitting has already run. Doing nothing.")
            return
        if not self.loaded_from_checkpoint:
            # Define initial training set
            if mpi.is_main_process:
                self.banner("Drawing initial samples.")
            self.do_initial_training()
            if mpi.is_main_process and self.verbose >= 4:
                print("Initial training set")
                print(self.gpr.training_set_as_df())
            # Check if any of the points in X_train are close to each other
            if len(self.gpr.X_train) > 1:
                distances = np.linalg.norm(
                    self.gpr.X_train[:, None] - self.gpr.X_train[None, :], axis=-1)
                if np.any(distances < 1e-10):
                    self.log("Warning: Some of the initial training points are very close "
                             "to each other. This may lead to numerical instability in "
                             "the GP. Consider increasing the number of initial points or "
                             "decreasing the volume of your prior.", level=1)
            if mpi.is_main_process:
                # Save checkpoint
                self.save_checkpoint()
        # Run bayesian optimization loop
        self.has_converged = False
        if mpi.is_main_process:
            maybe_stop_before_max_total = (
                (self.max_finite < self.max_total) or
                not isinstance(self.convergence[0], gpryconv.DontConverge))
            at_most_str = "at most " if maybe_stop_before_max_total else ""
        while (self.n_total_left > 0 and self.n_finite_left > 0 and
               not self.has_converged):
            self.current_iteration += 1
            self.progress.add_iteration()
            if mpi.is_main_process:
                n_iter_left = int(np.ceil(self.n_total_left / self.n_points_per_acq))
                self.banner(f"Iteration {self.current_iteration} "
                            f"({at_most_str}{n_iter_left} left)\n"
                            f"Total truth evals: {self.gpr.n_total} "
                            f"({self.gpr.n_finite} finite) of {self.max_total}" +
                            (f" (or {self.max_finite} finite)"
                             if self.max_finite < self.max_total else "") + "\n"
                )
            self.old_gpr = deepcopy(self.gpr)
            self.progress.add_current_n_truth(self.gpr.n_total, self.gpr.n_finite)
            # Acquire new points in parallel
            mpi.sync_processes()  # to sync the timer
            with TimerCounter(self.gpr) as timer_acq:
                force_resample = self.resamples > 0
                new_X, y_pred, acq_vals = self.acquisition.multi_add(
                    self.gpr,
                    n_points=self.n_points_per_acq,
                    random_state=self.random_state,
                    force_resample=force_resample,
                )
                # Check whether any of the points in new_X are either already in the
                # training set or exit multiple times in new_X
                if len(y_pred) > 0:
                    in_training_set, duplicates = check_candidates(self.gpr, new_X)
                    if mpi.is_main_process:
                        if np.any(in_training_set):
                            self.log(
                                f"{np.sum(in_training_set)} of the proposed points are "
                                "already in the training set. Skipping them."
                                , level=2
                            )
                        if np.any(duplicates):
                            self.log(
                                f"{np.sum(duplicates)} of the proposed points appear "
                                "multiple times. Skipping them.",
                                level=2
                            )
                    # make boolean mask of points to keep
                    keep = np.logical_not(np.logical_or(in_training_set, duplicates))
                    # TODO: test for points that will not add much: cut list when
                    #       acq(top) - acq(i) is large enough.
                    #       Maybe integrate in check_candidates
                    # # Do not evaluate points that are not expected to be useful
                    # delta_acq = 0.75
                    # i_small_acq = acq_vals - acq_vals[0] < -delta_acq
                    # make boolean mask of points to keep
                    # # keep = np.logical_not(
                    #     np.logical_or(
                    #         np.logical_or(in_training_set, duplicates), i_small_acq
                    #     )
                    # )
                    # Remove points that are not to be kept from new_X, y_pred, acq_vals
                    new_X = new_X[keep]
                    y_pred = y_pred[keep]
                    acq_vals = acq_vals[keep]
            self.progress.add_acquisition(timer_acq.time, timer_acq.evals)
            if mpi.is_main_process:
                self.log(f"[ACQUISITION] ({timer_acq.time:.2g} sec) Proposed {len(new_X)}"
                         " point(s) for truth evaluation.", level=3)
                self.log("New location(s) proposed, as [X, logp_gp(X), acq(X)]:", level=4)
                for X, y, acq in zip(new_X, y_pred, acq_vals):
                    self.log(f"   {X} {y} {acq}", level=4)
            # Checks how many candidates have been returned and if it's
            # less than half of the number requested (or less than 2 if only 2 requested),
            # force the acquisition to re-sample until either getting more points or
            # breaking if n_resamples_before_giveup is reached.
            if len(y_pred) < max(1, self.n_points_per_acq // 2):
                self.resamples += 1
                no_more_candidates = False
                if self.resamples > self.n_resamples_before_giveup:
                    if mpi.is_main_process:
                        self.log(
                            f"Acquisition returning no values after {self.resamples-1} "
                            "re-tries. Giving up.",
                            level=1,
                        )
                        no_more_candidates = True
                no_more_candidates = mpi.comm.bcast(no_more_candidates)
                if no_more_candidates:
                    break
                if mpi.is_main_process:
                    self.log("Acquisition returned less than half of the requested "
                             "points. Re-sampling ("
                             f"{self.n_resamples_before_giveup- self.resamples} "
                             "tries remaining)", level=2)
                continue
            self.resamples = 0
            mpi.sync_processes()  # to sync the timer
            with Timer() as timer_truth:
                # This call includes some overhead that will be added to the timer,
                # but it is very small for realistic true posteriors.
                new_y, eval_msg = self._eval_truth_parallel(new_X)
            if mpi.is_main_process:
                self.progress.add_truth(timer_truth.time, len(new_X))
                self.log(f"[EVALUATION] ({timer_truth.time:.2g} sec) {eval_msg}", level=3)
            mpi.sync_processes()
            # Add the newly evaluated truths to the GPR, and maybe refit hyperparameters.
            with TimerCounter(self.gpr) as timer_fit:
                fit_msg = self._fit_gpr_parallel(new_X, new_y)
            if mpi.is_main_process:
                self.progress.add_fit(timer_fit.time, timer_fit.evals_loglike)
                self.log(f"[FIT] ({timer_fit.time:.2g} sec) {fit_msg}", level=3)
                self.log(f"Current maximum log-posterior: {self.gpr.y_max}", level=3)
                self.log(f"Current GPR kernel: {self.gpr.kernel_}", level=3)
            mpi.sync_processes()
            # Share new_X, new_y and y_pred to the runner instance
            self.new_X, self.new_y, self.y_pred = mpi.comm.bcast(
                (new_X, new_y, y_pred) if mpi.is_main_process else (None, None, None))
            # We *could* check the max_total/finite condition and stop now, but it is
            # good to run the convergence criterion anyway, in case it has converged
            # Run the `callback` function
            # TODO: better failsafes for MPI_aware=False BUT actually using MPI
            # Use a with statement to pass an MPI communicator (dummy if MPI_aware=False)
            if self.callback:
                if self.callback_is_MPI_aware or mpi.is_main_process:
                    with Timer() as timer_callback:
                        self.callback(self)
                    if mpi.is_main_process:
                        self.log(f"[CALLBACK] ({timer_callback.time:.2g} sec) Evaluated "
                                 "the callback function.", level=3)
                mpi.sync_processes()
            # Calculate convergence and break if the run has converged
            mpi.sync_processes()
            with TimerCounter(self.gpr, self.old_gpr) as timer_convergence:
                has_converged = []
                for cc in self.convergence:
                    try:
                        has_converged.append(cc.is_converged_MPIwrapped(
                            self.gpr, self.old_gpr,
                            new_X, new_y, y_pred, self.acquisition))
                    except gpryconv.ConvergenceCheckError:
                        has_converged.append(False)
                convergence_policy = [cc.get_convergence_policy for cc in self.convergence]
                self.has_converged = has_converged[0]
                for i in range(1, len(has_converged)):
                    self.has_converged = self.has_converged and has_converged[i] \
                        if convergence_policy[i] == "and" else self.has_converged or has_converged[i]
                if mpi.is_main_process:
                    print(f"has_converged = {self.has_converged}, {has_converged}")
                    print(f"convergence_policy = {convergence_policy}")
                mpi.sync_processes()
            self.progress.add_convergence(
                timer_convergence.time, timer_convergence.evals,
                [cc.last_value for cc in self.convergence]
            )
            mpi.share_attr(self, "has_converged")
            if mpi.is_main_process:
                last_values = ", ".join(
                    f"{cc.last_value:.2g} (limit {cc.limit:.2g})"
                    for cc in self.convergence
                )
                self.log(f"[CONVERGENCE] ({timer_convergence.time:.2g} sec) "
                         "Evaluated convergence criterion to " + last_values, level=2)
            mpi.sync_processes()
            # TODO: uncomment for mean and cov updates (cov would be used for corr.length)
            self.update_mean_cov()
            self.progress.mpi_sync()
            self.save_checkpoint()
            if mpi.is_main_process and self.plots:
                try:
                    self.plot_progress(
                        **(self.plots if isinstance(self.plots, Mapping) else {})
                    )
                except Exception as excpt:
                    self.log(f"Failed to plot progress: {excpt}", level=2)
        else:  # check "while" ending condition
            mpi.sync_processes()
            if mpi.is_main_process:
                lines = "Finished!\n"
                if self.has_converged:
                    lines += "- The run has converged.\n"
                if self.n_total_left <= 0:
                    lines += ("- The maximum number of truth evaluations "
                              f"({self.max_total}) has been reached.\n")
                if self.max_finite < self.max_total and self.n_finite_left <= 0:
                    lines += ("- The maximum number of finite truth evaluations "
                              f"({self.max_finite}) has been reached.")
                if self.resamples > self.n_resamples_before_giveup:
                    lines += (
                        f"- Gave up up after {self.resamples-1} resamples "
                        f"(max. {self.n_resamples_before_giveup})."
                    )
                self.banner(lines)
            if self.diagnosis:
                self.diagnose()
        self.has_run = True

    def logpost_eval_and_report(self, X, level=None):
        """
        Simple wrapper to evaluate and return the true log-posterior at X, and log it
        with the given ``level``.
        """
        self.log(f"[{mpi.RANK}] Evaluating true posterior at\n{X}", level=level)
        logp = self.model.logpost(X)
        self.log(f"[{mpi.RANK}] --> log(p) = {logp}", level=4)
        return logp

    def do_initial_training(self):
        """
        Draws an initial sample for the `gpr` GP model until it has a training set of size
        `n_initial`, counting only finite-target points ("finite" here meaning over the
        threshold of the SVM classifier, if present).

        This function is MPI-aware and broadcasts the initialized GPR to all processes.
        """
        self.progress.add_iteration()
        self.progress.add_current_n_truth(0, 0)
        self.progress.add_acquisition(0, 0)
        self.progress.add_convergence(0, 0, [np.nan] * len(self.convergence))
        # Check if there's an SVM and if so read out it's threshold value
        # We will compare it against y - max(y)
        if isinstance(self.gpr.infinities_classifier, SVM):
            # Check by hand against the threshold (in the non-transformed space)
            is_finite = lambda ymax_minus_y: (
                self.gpr.infinities_classifier._is_finite_raw(
                    -ymax_minus_y, self.gpr.diff_threshold, max_y=0
                )
            )
        else:
            is_finite = np.isfinite
        if mpi.is_main_process:
            # Check if the GP already contains points. If so they are reused.
            pretrained = 0
            # Arrays to store the initial sample
            X_init = np.empty((0, self.d))
            y_init = np.empty(0)
            if hasattr(self.gpr, "y_train"):
                if len(self.gpr.y_train) > 0:
                    pretrained = len(self.gpr.y_train)
                    X_init = self.gpr.X_train
                    y_init = self.gpr.y_train
            n_still_needed = np.max([0, self.n_initial - pretrained])
            n_to_sample_per_process = int(np.ceil(n_still_needed / mpi.SIZE))
        if mpi.multiple_processes:
            n_to_sample_per_process = mpi.comm.bcast(
                n_to_sample_per_process if mpi.is_main_process else None)
        if n_to_sample_per_process == 0 and self.verbose > 1:  # Enough pre-training
            warnings.warn("The number of pretrained points exceeds the number of "
                          "initial samples")
            return
        n_iterations_before_giving_up = int(
            np.ceil(self.max_initial / n_to_sample_per_process))
        # Initial samples loop. The initial samples are drawn from the prior
        # and according to the distribution of the prior.
        mpi.sync_processes()  # to sync the timer
        with Timer() as timer_truth:
            if mpi.is_main_process:
                progress_bar = tqdm(total=n_still_needed)
            for i in range(n_iterations_before_giving_up):
                X_init_loop = np.empty((0, self.d))
                y_init_loop = np.empty(0)
                for j in range(n_to_sample_per_process):
                    # Draw a point from prior and evaluate logposterior at that point.
                    # But check first if the point is within the priors.
                    X_in_bounds = False
                    proposer_tries = 0
                    warn_multiple = 10 * self.gpr.d
                    while not X_in_bounds:
                        X = self.initial_proposer.get(random_state=self.random_state)
                        X_in_bounds = is_in_bounds(X, self.prior_bounds)[0]
                        proposer_tries += 1
                        if proposer_tries > 0 and proposer_tries > warn_multiple:
                            self.log(
                                "The initial proposer is having trouble finding "
                                "points within the prior bounds "
                                f"(#attempts={proposer_tries}). Consider changing "
                                "the initial proposer or the prior bounds.",
                                level=1
                            )
                    y = self.logpost_eval_and_report(X, level=4)
                    X_init_loop = np.append(X_init_loop, np.atleast_2d(X), axis=0)
                    y_init_loop = np.append(y_init_loop, y)
                # Gather points and decide whether to break.
                if mpi.multiple_processes:
                    # GATHER keeps rank order (MPI standard): we can do X and y separately
                    all_points = mpi.comm.gather(X_init_loop)
                    all_posts = mpi.comm.gather(y_init_loop)
                else:
                    all_points = [X_init_loop]
                    all_posts = [y_init_loop]
                if mpi.is_main_process:
                    X_init = np.concatenate([X_init, np.concatenate(all_points)])
                    y_init = np.concatenate([y_init, np.concatenate(all_posts)])
                    # Only finite values contribute to the number of initial samples
                    n_finite_new = sum(is_finite(max(y_init) - y_init))
                    # NB: tqdm.update takes *increments*
                    progress_bar.update(n_finite_new - progress_bar.n)
                    # Break loop if the desired number of initial samples is reached
                    finished = n_finite_new >= n_still_needed
                if mpi.multiple_processes:
                    finished = mpi.comm.bcast(finished if mpi.is_main_process else None)
                if finished:
                    break
                else:
                    # TODO: maybe re-fit SVM to shrink initial sample region
                    pass
        if mpi.is_main_process:
            progress_bar.close()
        if self.progress and mpi.is_main_process:
            self.progress.add_truth(timer_truth.time, len(X_init))
        if mpi.is_main_process:
            self.log(f"[EVALUATION] ({timer_truth.time:.2g} sec) "
                     f"Evaluated the true model at {len(X_init)} location(s)"
                     f", of which {n_finite_new} returned a finite value." +
                     (" Each MPI process evaluated at most "
                      f"{max(len(p) for p in all_points)} locations."
                      if mpi.multiple_processes else ""), level=3)
        if mpi.is_main_process:
            # Raise error if the number of initial samples hasn't been reached
            if not finished:
                raise RuntimeError(
                    f"The desired number of finite initial samples ({n_still_needed}) "
                    f"has not been reached after {len(X_init)} evaluations. Try "
                    "increasing the amount of max initial evaluations `max_initial`, or "
                    "decreasing the volume of the prior.")
            # Append the initial samples to the gpr
            with TimerCounter(self.gpr) as timer_fit:
                self.gpr.append_to_data(X_init, y_init, fit_gpr=True)
            self.progress.add_fit(timer_fit.time, timer_fit.evals_loglike)
            self.log(f"[FIT] ({timer_fit.time:.2g} sec) Fitted GP model with new acquired"
                     " points, including GPR hyperparameters. "
                     f"{self.gpr.n_last_appended_finite} finite points were added to the "
                     "GPR.", level=3)
            self.log(f"Current GPR kernel: {self.gpr.kernel_}", level=4)
        # Broadcast results
        self._share_gpr()
        self.progress.mpi_sync()

    def _eval_truth_parallel(self, new_X):
        """
        Performs the evaluation of the true model in parallel.

        Returns all y's at rank 0 (None otherwise), and a short report msg.
        """
        # Select locations that will be evaluated by this process
        n_evals_per_process = mpi.split_number_for_parallel_processes(len(new_X))
        n_this_process = n_evals_per_process[mpi.RANK]
        i_this_process = sum(n_evals_per_process[:mpi.RANK])
        new_X_this_process = new_X[i_this_process: i_this_process + n_this_process]
        # Perform the evaluations
        new_y_this_process = np.empty(0)
        for x in new_X_this_process:
            logp = self.logpost_eval_and_report(x, level=4)
            new_y_this_process = np.append(new_y_this_process, logp)
        # Collect (if parallel) and append to the current model
        if mpi.multiple_processes:
            # GATHER keeps rank order (MPI standard): we can do X and y separately
            new_Xs = mpi.comm.gather(new_X_this_process)
            new_ys = mpi.comm.gather(new_y_this_process)
            if mpi.is_main_process:
                new_X = np.concatenate(new_Xs)
                new_y = np.concatenate(new_ys)
            new_X, new_y = mpi.comm.bcast(
                (new_X, new_y) if mpi.is_main_process else (None, None))
        else:
            new_y = new_y_this_process
        eval_msg = None
        if mpi.is_main_process:
            eval_msg = (
                f"Evaluated the true model at {len(new_X)} location(s)" +
                (f" (at most {len(new_X_this_process)} per MPI process)"
                 if mpi.multiple_processes else "") +
                f", of which {sum(np.isfinite(new_y))} returned a finite value."
            )
        return new_y, eval_msg

    def _fit_gpr_parallel(self, new_X, new_y, fit="full"):
        # Prepare hyperparameter fit
        hyperparams_bounds = None
        # if self.cov is not None:
        #     stds = np.sqrt(np.diag(self.cov))
        #     prior_bounds = self.model.prior.bounds(confidence_for_unbounded=0.99995)
        #     relative_stds = stds / (prior_bounds[:, 1] - prior_bounds[:, 0])
        #     new_bounds = np.array([relative_stds / 2,  relative_stds * 2]).T
        #     hyperparams_bounds = self.gpr.kernel_.bounds.copy()
        #     hyperparams_bounds[1:] = np.log(new_bounds)
        fit_gpr_kwargs = {
            "hyperparameter_bounds": mpi.comm.bcast(hyperparams_bounds),
            "start_from_current": mpi.is_main_process,
        }
        is_this_iter = lambda every: self.current_iteration % every == every -1
        if self.fit_full_every and is_this_iter(self.fit_full_every):
            fit_gpr_kwargs["n_restarts"] = mpi.split_number_for_parallel_processes(
                self.gpr.n_restarts_optimizer
            )[mpi.RANK]
        elif self.fit_simple_every and is_this_iter(self.fit_simple_every):
            fit_gpr_kwargs["n_restarts"] = 1
        else:
            fit_gpr_kwargs["n_restarts"] = 0
        # At lest rank 0 must run, even if not fitting the GPR/SVM, to add the points
        if fit_gpr_kwargs['n_restarts'] or mpi.is_main_process:
            what_hyper = (
                f"fit with {fit_gpr_kwargs['n_restarts']} restart(s) per MPI process." if
                fit_gpr_kwargs['n_restarts'] else "kept constant."
            )
            self.log(
                f"[{mpi.RANK}] Fitting log(p) surrogate model. "
                "GPR hyperparameters will be " + what_hyper,
                level=4,
            )
            self.gpr.append_to_data(
                new_X, new_y, fit_classifier=True,
                # Supresses warning:
                fit_gpr=(fit_gpr_kwargs if fit_gpr_kwargs['n_restarts'] else False),
            )
            lml = self.gpr.log_marginal_likelihood_value_
            self.log(f"[{mpi.RANK}] --> Got best log-marginal-likelihood {lml}", level=4)
        else:
            self.log(
                f"[{mpi.RANK}] No hyperparameter fitting runs assigned to this process.",
                level=4,
            )
            lml = -np.inf
        # Pick best and share it
        lmls = mpi.comm.allgather((mpi.RANK, lml))
        best_i = lmls[np.argmax([l for i, l in lmls])][0]
        if mpi.is_main_process:
            self.log(
                f"[{mpi.RANK}] Overall best log-marginal-likelihood {lmls[best_i][1]}",
                level=4,
            )
        self._share_gpr(root=best_i)
        msg = None
        if mpi.is_main_process:
            msg = (
                f"Fitted log(p) surrogate model with {self.gpr.n_last_appended} new "
                f"points, of which {self.gpr.n_last_appended} were added to the GPR. "
                f"GPR hyperparameters were " + what_hyper
            )
        return msg

    def update_mean_cov(self):
        """
        Updates and shares mean and cov if available, checking GPAcquisition first, and
        Convergence second if not present in GPAcquisition.
        """
        for attr in ["mean", "cov"]:
            if mpi.is_main_process:
                value = getattr(self.acquisition, attr, None)
                if value is None:
                    value = getattr(self.convergence, attr, None)
                setattr(self, attr, value)
            mpi.share_attr(self, attr)

    def plot_progress(
            self,
            format="svg",
            timing=True,
            convergence=True,
            trace=True,
            slices=False,
    ):
        """
        Creates some progress plots and saves them at path (assumes path exists).

        Parameters
        ----------
        format : str (default ``"svg"``)
            Format for the plots, among the available ones in ``matplotlib``.

        timing : bool (default: True)
            Plot histogram of timing per iteration (totals in legend).

        convergence : bool (default: True)
            Plot the evolution of the convergence criterion (included in ``trace`` plot).

        trace : bool (default: True)
            Plot the evolution of the run: convergence criterion, surrogate log(p) and
            parameters.

        slices : bool (default: False)
            Plots slices per training samples (slow, use for diagnosis only).
        """
        if not mpi.is_main_process:
            return
        self.ensure_paths(plots=True)
        import matplotlib.pyplot as plt  # pylint: disable=import-outside-toplevel
        if timing:
            self.progress.plot_timing(
                truth=True, save=os.path.join(self.plots_path, f"timing.{format}")
            )
        if convergence:
            fig, ax = gpplt.plot_convergence(self.convergence)
            plt.savefig(os.path.join(self.plots_path, f"convergence.{format}"))
        if trace:
            gpplt.plot_trace(
                self.model, self.gpr, self.convergence, self.progress,
                reference=self.last_mc_samples()
            )
            plt.savefig(os.path.join(self.plots_path, f"trace.{format}"))
        if slices:
            gpplt.plot_slices(self.model, self.gpr, self.acquisition)
            plt.savefig(os.path.join(self.plots_path, f"slices.{format}"))
        plt.close("all")

    def generate_mc_sample(
            self, sampler="mcmc", output=None, add_options=None, resume=False
    ):
        """
        Runs an MC process using `Cobaya <https://cobaya.readthedocs.io/en/latest/sampler.html>`_.

        The result can be retrieved using the ``last_mc_samples`` method.

        Parameters
        ----------
        sampler : string (default `"mcmc"`) or dict
            Sampler to be initialised. If a string, it must be `"mcmc"` or `"polychord"`.
            It can also be a dict as ``{sampler: {option: value, ...}}``, containing a
            full sampler definition, see `here
            <https://cobaya.readthedocs.io/en/latest/sampler.html>`_. In this case, any
            sampler understood by Cobaya can be used.

        add_options : dict, optional
            Dict of additional options to be passed to the sampler.

        output: path, optional (default: ``checkpoint/chains``, if ``checkpoint != None``)
            The path where the resulting Monte Carlo sample shall be stored. If passed
            explicitly ``False``, produces no output.

        resume: bool, optional (default=False)
            Whether to resume from existing output files (True) or force overwrite (False)
        """
        if not self.gpr.fitted:
            raise Exception("You have to have added points to the GPR "
                            "before you can generate an mc_sample")
        if output is None and self.checkpoint is not None:
            output = os.path.join(self.checkpoint, "chains/mc_samples")
        # Add a covariance matrix if it exists (e.g. from MC-based acquisition)
        if self.cov is not None and "covmat" not in (add_options or {}):
            if add_options is None:
                add_options = {}
            add_options["covmat"] = self.cov
            # No need to specify parameter names: same order bc same model param info
        # Update the ref to the available info
        # TODO: unused at the moment
        best_point_per_mpi_rank = \
            self.gpr.X_train[np.argsort(self.gpr.y_train)[-1 + mpi.RANK]]
        ref = {
            p: val for p, val in zip(
                self.model.parameterization.sampled_params(), best_point_per_mpi_rank
            )
        }
        # Hack: reuse shrunk bounds from acquisition class
        # TODO: change this so that shrunk bounds are used at GPR model level
        bounds = None
        if hasattr(self.acquisition, "shrink_priors"):
            bounds = self.acquisition.shrink_priors(self.gpr)
        # end of hack
        self.last_mc_surr_info, self.last_mc_sampler = mc_sample_from_gp(
            self.gpr, true_model=self.model, sampler=sampler, bounds=bounds,
            convergence=self.convergence, output=output, add_options=add_options,
            resume=resume, verbose=self.verbose)
        sampler_name = sampler if isinstance(sampler, str) else list(sampler)[0]
        self._last_mc_samples = self.last_mc_sampler.samples(
            combined=True,
            skip_samples=0.33 if sampler_name.lower() == "mcmc" else 0
        )
        mpi.share_attr(self, "_last_mc_samples")
        return self._last_mc_samples

    def last_mc_samples(self, as_getdist=True):
        """
        Returns the last MC sample available from the surrogate model, if any has been
        generated.

        If ``as_getdist=True`` (default), they are returned as a
        :class:`getdist.MCSamples` instance. Otherwise as
        :class:`cobaya.SampleCollection`.
        """
        if as_getdist and self._last_mc_samples is not None:
            return self._last_mc_samples.to_getdist(model=self.model)
        return self._last_mc_samples

    # TODO: recover, for tests about sampling overshoots
    def diagnose(self):
        if mpi.is_main_process:
            lines = "Starting diagnosis\n"
            lines += "- Evaluating corners"
            self.log(lines)
            bounds = self.model.prior.bounds()
            ndim = len(bounds)
            mesh = np.meshgrid(*bounds)
            corners = np.stack(mesh, axis=-1).reshape(-1, ndim)
            # Evaluate GP at all corners
            vals_in_corners = self.gpr.predict(corners, validate=False)
            # Check if at any point it's overshooting
            higher_than_max = vals_in_corners > self.gpr.y_max
            if np.sum(higher_than_max) > 0:
                lines = f"WARNING: found {np.sum(higher_than_max)} corners\n"
                lines += "where the GP predicts a higher value than its\n"
                lines += "maximum. Reevaluating those corners..."
                self.log(lines)
                # Filter the points where the high values are predicted and
                # evaluate the posterior distribution there
                points_to_evaluate = np.atleast_2d(corners[higher_than_max])
                new_vals = np.empty(len(points_to_evaluate))
                for i, p in enumerate(points_to_evaluate):
                    new_vals[i] = self.model.logpost(p)
                    self.gpr.append_to_data(points_to_evaluate, new_vals,
                            fit=True)
                self._share_gpr()
                # self.save_checkpoint()
                self.log("...done.")

    # pylint: disable=import-outside-toplevel
    def plot_mc(self, samples_or_samples_folder=None, add_training=True,
                add_samples=None, output=None, output_dpi=200, format="svg"):
        """
        Creates a triangle plot of an MC sample of the surrogate model, and optionally
        shows some evaluation locations.

        Parameters
        ----------
        samples_or_samples_folder : cobaya.SampleCollection, getdist.MCSamples, str
            MC samples returned by a call to the :func:`Runner.generate_mc_sample`
            method, the output path where they were written, or a getdist.MCSamples
            instance. If not specified (default) it will try to use the last set of
            samples generated.

        add_training : bool, optional (default=True)
            Whether the training locations are plotted on top of the contours.

        add_samples : dict(label, (cobaya.SampleCollection, getdist.MCSamples, str))
            Extra MC samples to be added to the plot, specified as dict with labels as
            keys, and the same type as ``samples_or_samples_folder`` as values.
            Default: None.

        output : str or os.path, optional (default=None)
            The location to save the generated plot in. If ``None`` it will be saved in
            ``checkpoint_path/images/Surrogate_triangle.pdf`` or
            ``./images/Surrogate_triangle.png`` if ``checkpoint_path`` is ``None``

        output_dpi : int (default: 200)
            The resolution of the generated plot in DPI.

        format : str (default: "svg" if `output` not defined; else ignore)
        """
        if not mpi.is_main_process:
            warnings.warn(
                "Running plotting function from non-root MPI process. Doing nothing."
            )
            return
        base_label = f"MC samples from GP ({len(self.gpr.X_train_all)} evals.)"
        if samples_or_samples_folder is None:
            if self._last_mc_samples is None:
                raise ValueError(
                    "No MC samples have been obtained for this Runner. You need to run "
                    "the generate_mc_sample() method first, or pass samples or a path "
                    "to them as first argument."
                )
            gdsamples_dict = {base_label: self.last_mc_samples()}
        else:
            gdsamples_dict = {base_label: samples_or_samples_folder}
        if add_samples is None:
            add_samples = {}
        elif not isinstance(add_samples, Mapping):
            add_samples = {"Add. samples": add_samples}
        gdsamples_dict.update(add_samples)
        gdsamples_dict = process_gdsamples(gdsamples_dict)
        import getdist.plots as gdplt
        from gpry.plots import getdist_add_training
        import matplotlib.pyplot as plt
        self.ensure_paths(plots=True)
        gdplot = gdplt.get_subplot_plotter(subplot_size=2)
        gdplot.settings.line_styles = 'tab10'
        gdplot.settings.solid_colors = 'tab10'
        gdplot.triangle_plot(
            list(gdsamples_dict.values()), self.model.parameterization.sampled_params(),
            filled=True, legend_labels=list(gdsamples_dict))
        if add_training and self.d > 1:
            getdist_add_training(gdplot, self.model, self.gpr)
        if output is None:
            output = os.path.join(self.plots_path, f"Surrogate_triangle.{format}")
        plt.savefig(output, dpi=output_dpi)
        return gdplot

    def plot_distance_distribution(
            self, samples_or_samples_folder=None, show_added=True, output=None,
            output_dpi=200, format="svg"):
        """
        Plots the distance distribution of the training points with respect to the
        confidence ellipsoids (in a Gaussian approx) derived from an MC sample of the
        surrogate model.

        Parameters
        ----------
        samples_or_samples_folder : cobaya.SampleCollection, getdist.MCSamples, str
            MC samples returned by a call to the :func:`Runner.generate_mc_sample`
            method, the output path where they were written, or a getdist.MCSamples
            instance. If not specified (default) it will try to use the last set of
            samples generated.

        show_added: bool (default True)
            Colours the stacks depending on how early or late the corresponding points
            were added (bluer stacks represent newer points).

        output : str or os.path, optional (default=None)
            The location to save the generated plot in. If ``None`` it will be saved in
            ``.png`` format at ``checkpoint_path/images/``, or ``./images/`` if
            ``checkpoint_path`` was ``None``.

        output_dpi : int (default: 200)
            The resolution of the generated plot in DPI.

        format : str (default: "svg" if `output` not defined; else ignore)
        """
        if not mpi.is_main_process:
            warnings.warn(
                "Running plotting function from non-root MPI process. Doing nothing."
            )
            return
        if samples_or_samples_folder is None:
            if self._last_mc_samples is None:
                raise ValueError(
                    "No MC samples have been obtained for this Runner. You need to run "
                    "the generate_mc_sample() method first, or pass samples or a path "
                    "to them as first argument."
                )
            gdsample = self.last_mc_samples(as_getdist=True)
        else:
            gdsample = samples_or_samples_folder
        gdsample = list(process_gdsamples({None: gdsample}).values())[0]
        n_params = len(self.model.parameterization.sampled_params())
        mean = gdsample.getMeans()[:n_params]
        covmat = gdsample.getCovMat().matrix[:n_params, :n_params]
        self.ensure_paths(plots=True)
        if output is None:
            output_1 = os.path.join(
                self.plots_path, f"Distance_distribution.{format}"
            )
            output_2 = os.path.join(
                self.plots_path, f"Distance_distribution_density.{format}"
            )
        else:
            output_1 = output
            # We need to change the 2nd NameError
            name, ext = os.path.splitext(output)
            output_2 = name + "_density" + ext
        import matplotlib.pyplot as plt  # pylint: disable=import-outside-toplevel
        fig, ax = gpplt.plot_distance_distribution(
            self.gpr, mean, covmat, density=False, show_added=show_added)
        plt.savefig(output_1, dpi=output_dpi)
        fig, ax = gpplt.plot_distance_distribution(
            self.gpr, mean, covmat, density=True, show_added=show_added)
        plt.savefig(output_2, dpi=output_dpi)
