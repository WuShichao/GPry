# general purpose stuff
import warnings
from copy import deepcopy
from operator import itemgetter
from typing import Mapping

# numpy and scipy
import numpy as np
from scipy.linalg import cholesky, solve_triangular, cho_solve
from scipy.linalg.blas import dtrmm as tri_mul
import scipy.optimize

# sklearn GP and kernel utilities
from sklearn.gaussian_process import GaussianProcessRegressor \
    as sk_GaussianProcessRegressor
from sklearn.base import clone, BaseEstimator as BE

# sklearn utilities
from sklearn.utils.validation import check_array

# gpry kernels and SVM
from gpry.kernels import RBF, Matern, ConstantKernel as C
from gpry.svm import SVM
from gpry.preprocessing import Normalize_bounds
from gpry.tools import check_random_state, get_Xnumber, nstd_of_1d_nstd


class GaussianProcessRegressor(sk_GaussianProcessRegressor, BE):
    r"""
    GaussianProcessRegressor (GPR) that allows dynamic expansion.

    The implementation is based on Algorithm 2.1 of Gaussian Processes
    for Machine Learning (GPML) by Rasmussen and Williams.

    In addition to standard scikit-learn estimator API,
    GaussianProcessRegressor:

       * allows prediction without prior fitting (based on the GP prior).
       * implements a pipeline for pretransforming data before fitting.
       * provides the method :meth:`append_to_data` which allows to append
         additional data points to an already existing GPR. This is done either
         by refitting the hyperparameters (theta) or alternatively by using the
         Matrix inversion Lemma to keep the hyperparameters fixed.
       * overwrites the (hidden) native deepcopy function. This enables copying
         the GPR as well as the sampled points it contains.

    Parameters
    ----------
    kernel : kernel object, string, dict, optional (default: "RBF")
        The kernel specifying the covariance function of the GP. If
        "RBF"/"Matern" is passed, the asymmetric kernel::

            ConstantKernel() * RBF/Matern()

        is used as default where ``n_dim`` is the number of dimensions of the
        training space. In this case you will have to provide the prior bounds
        as the ``bounds`` parameter to the GP. For the Matern kernel
        :math:`\nu=3/2`. To pass different arguments to the kernel, e.g. ``nu=5/2``
        for Matern, pass a single-key dict as ``{"Matern": {"nu": 2.5}}"``. Note
        that the kernel's hyperparameters are optimized during fitting.

    noise_level : float or array-like, optional (default: 1e-2)
        Square-root of the value added to the diagonal of the kernel matrix
        during fitting. Larger values correspond to increased noise level in the
        observations and reduce potential numerical issue during fitting.
        If an array is passed, it must have the same number of entries as the
        data used for fitting and is used as datapoint-dependent noise level.
        Note that this is equivalent to adding a WhiteKernel with c=noise_level.

    optimizer : str or callable, optional (default: "fmin_l_bfgs_b")
        Can either be one of the internally supported optimizers for optimizing
        the kernel's parameters, specified by a string, or an externally
        defined optimizer passed as a callable. If a callable is passed, it
        must have the signature::

            def optimizer(obj_func, initial_theta, bounds):
                # * 'obj_func' is the objective function to be maximized, which
                #   takes the hyperparameters theta as parameter and an
                #   optional flag eval_gradient, which determines if the
                #   gradient is returned additionally to the function value
                # * 'initial_theta': the initial value for theta, which can be
                #   used by local optimizers
                # * 'bounds': the bounds on the values of theta
                ....
                # Returned are the best found hyperparameters theta and
                # the corresponding value of the target function.
                return theta_opt, func_min

        Per default, the 'fmin_l_bfgs_b' algorithm from scipy.optimize
        is used. If None is passed, the kernel's parameters are kept fixed.
        Available internal optimizers are::

            'fmin_l_bfgs_b'

    n_restarts_optimizer : int, optional (default: 0)
        The number of restarts of the optimizer for finding the kernel's
        parameters which maximize the log-marginal likelihood. The first run
        of the optimizer is performed from the kernel's initial parameters,
        the remaining ones (if any) from thetas sampled log-uniform randomly
        from the space of allowed theta-values. If greater than 0, all bounds
        must be finite. Note that n_restarts_optimizer == 0 implies that one
        run is performed.

    preprocessing_X : X-preprocessor, Pipeline_X, optional (default: None)
        Single preprocessor or pipeline of preprocessors for X. If None is
        passed the data is not preprocessed. The `fit` method of the
        preprocessor is only called when the GP's hyperparameters are refit.

    preprocessing_y : y-preprocessor or Pipeline_y, optional (default: None)
        Single preprocessor or pipeline of preprocessors for y. If None is
        passed the data is not preprocessed. The `fit` method of the preprocessor
        is only called when the GP's hyperparameters are refit.

    account_for_inf : SVM, None or "SVM" (default: "SVM")
        Uses a SVM (Support Vector Machine) to classify the data into finite and
        infinite values. This allows the GP to express values of -inf in the
        data (unphysical values). If all values are finite the SVM will just
        pass the data through itself and do nothing.

    inf_threshold : None, float or str
        Threshold for the infinities classifier to consider a value finite, understood as
        a positive difference with respect to the current maximum y. It can be given as a
        string formed of a number and ending in "s", meaning the distance to the mode
        which shall be considered finite in :math:`\sigma` using a :math:`\chi^2`
        distribution. Used only if account_for_inf is not None.

    bounds : array-like, shape=(n_dims,2), optional
        Array of bounds of the prior [lower, upper] along each dimension. Has
        to be provided when the kernel shall be built automatically by the GP.

    random_state : int or `numpy RandomState <https://numpy.org/doc/stable/reference/random/legacy.html?highlight=randomstate#numpy.random.RandomState>`_, optional
        The generator used to initialize the centers. If an integer is
        given, it fixes the seed. Defaults to the global numpy random
        number generator.

    verbose : 1, 2, 3, optional (default: 1)
        Level of verbosity of the GP. 3 prints Infos, Warnings and Errors, 2
        Warnings and Errors, and 1 only Errors. Should be set to 2 or 3 if
        problems arise.

    Attributes
    ----------
    n : int
        Number of points/features in the training set of the GPR.

    d : int
        Dimensionality of the training data.

    X_train : array-like, shape = (n_samples, n_features)
        Original (untransformed) feature values in training data of the GPR. Intended to
        be used when one wants to access the training data for any purpose.

    y_train : array-like, shape = (n_samples, [n_output_dims])
        Original (untransformed) target values in training data of the GPR. Intended to be
        used when one wants to access the training data for any purpose.

    X_train_ : array-like, shape = (n_samples, n_features)
        (Possibly transformed) feature values in training data of the GPR (also required
        for prediction). Mostly intended for internal use.

    y_train_ : array-like, shape = (n_samples, [n_output_dims])
        (Possibly transformed) target values in training data of the GPR (also required
        for prediction). Mostly intended for internal use.

    n_total : int
        Number of points/features in the training set of the model, including points with
        target values classified as infinite.

    X_train_all : array-like, shape = (n_samples, n_features)
        Original (untransformed) feature values in training data of the model, including
        points with target values classified as infinite, and thus not part of the
        training set of the GPR.

    y_train_all : array-like, shape = (n_samples, [n_output_dims])
        Original (untransformed) target values in training data of the model, including
        values classified as infinite, and thus not part of the training set of the GPR.

    X_train_all_ : array-like, shape = (n_samples, n_features)
        (Possibly transformed) feature values in training data of the model, including
        points with target values classified as infinite.

    y_train_all_ : array-like, shape = (n_samples, [n_output_dims])
        (Possibly transformed) target values in training data of the GPR model, including
        points with target values classified as infinite.

    noise_level : array-like, shape = (n_samples, [n_output_dims]) or scalar
        The noise level (square-root of the variance) of the uncorrelated
        training data. This is un-transformed.

    noise_level_ : array-like, shape = (n_samples, [n_output_dims]) or scalar
        The transformed noise level (if y is preprocessed)

    alpha : array-like, shape = (n_samples, [n_output_dims]) or scalar
        The value which is added to the diagonal of the kernel. This is the
        square of `noise_level`.

    kernel_ : :mod:`kernels` object
        The kernel used for prediction. The structure of the kernel is the
        same as the one passed as parameter but with optimized hyperparameters.

    alpha_ : array-like, shape = (n_samples, n_samples)
        **Not to be confused with alpha!** The inverse Kernel matrix of the
        training points multiplied with ``y_train_`` (Dual coefficients of
        training data points in kernel space). Needed at prediction.

    V_ : array-like, shape = (n_samples, n_samples)
        Lower-triangular Cholesky decomposition of the inverse kernel in ``X_train_``

        .. warning::

            ``L_`` is not recomputed when using the append_to_data method
            without refitting the hyperparameters. As only ``K_inv_`` and
            ``alpha_`` are used at prediction this is not neccessary.

    log_marginal_likelihood_value_ : float
        The log-marginal-likelihood of ``self.kernel_.theta``


    **Methods:**

    .. autosummary::
        :toctree: stubs

        append_to_data
        fit
        _update_model
        predict
    """

    def __init__(self, kernel="RBF", noise_level=1e-2,
                 optimizer="fmin_l_bfgs_b", n_restarts_optimizer=0,
                 preprocessing_X=None, preprocessing_y=None,
                 account_for_inf="SVM", inf_threshold="20s", bounds=None,
                 random_state=None, verbose=1):
        self.n_last_appended = 0
        self.n_last_appended_finite = 0
        self.newly_appended_for_inv = 0
        self.preprocessing_X = preprocessing_X
        self.preprocessing_y = preprocessing_y
        self.noise_level = noise_level
        self.n_eval = 0
        self.n_eval_loglike = 0
        self.verbose = verbose
        self.inf_value = np.inf
        self.minus_inf_value = -np.inf
        self._fitted = False
        self.bounds = bounds
        # Initialize SVM if necessary
        self.inf_threshold = inf_threshold
        if account_for_inf == "SVM":
            self.infinities_classifier = SVM(random_state=random_state)
        elif account_for_inf is False:
            self.infinities_classifier = None
        else:
            self.infinities_classifier = account_for_inf
        if self.infinities_classifier is not None:
            y_preprocessor_guaranteed_linear = (
                self.preprocessing_y is None or
                getattr(self.preprocessing_y, "is_linear", False)
            )
            if not y_preprocessor_guaranteed_linear:
                warnings.warn(
                    "If using a standard classifier for infinities, the y-preprocessor "
                    "needs to be linear (declare an attr ``is_linear=True``). This may "
                    "lead to errors further in the pipeline."
                )
            if self.inf_threshold is None:
                raise ValueError(
                    "Specify 'inf_threshold' if using infinities classifier."
                )
            value, sigma_units = get_Xnumber(
                self.inf_threshold, "s", None, dtype=float, varname="inf_threshold"
            )
            if sigma_units:  # sigma units
                self.diff_threshold = self.compute_threshold_given_sigma(value, self.d)
            else:
                self.diff_threshold = value
        # Auto-construct inbuilt kernels
        if isinstance(kernel, str):
            kernel = {kernel: {}}
        if isinstance(kernel, Mapping):
            if len(kernel) != 1:
                raise ValueError("'kernel' must be a single-key dict.")
            kernel_name = list(kernel)[0]
            kernel_args = kernel[kernel_name] or {}
            if self.bounds is None:
                raise ValueError("You selected used the automatically "
                                 f"constructed '{kernel_name}' kernel without "
                                 "specifying prior bounds.")
            # Transform prior bounds if neccessary
            if self.preprocessing_X is not None:
                self.bounds_ = self.preprocessing_X.transform_bounds(self.bounds)
            else:
                self.bounds_ = self.bounds
            # Check if it's a supported kernel
            try:
                length_corr_kernel = {"rbf": RBF, "matern": Matern}[kernel_name.lower()]
            except KeyError as excpt:
                raise ValueError(
                    "Currently only 'RBF' and 'Matern' are "
                    f"supported as standard kernels. Got '{kernel_name}'."
                ) from excpt
            # Build kernel
            kernel = C(1.0, [0.001, 10000]) \
                * length_corr_kernel([0.01] * self.d, "dynamic",
                                     prior_bounds=self.bounds_, **kernel_args)
        sk_GaussianProcessRegressor.__init__(
            self, kernel=kernel, alpha=noise_level**2., optimizer=optimizer,
            n_restarts_optimizer=n_restarts_optimizer,
            normalize_y=False, copy_X_train=True,
            random_state=random_state
        )
        if self.verbose >= 3:
            print("Initializing GP with the following options:")
            print("===========================================")
            print("* Kernel:")
            print("   ", self.kernel)
            print("  with hyperparameters:")
            for h in self.kernel.hyperparameters:
                print("    -", h)
            print(f"* Noise level: {noise_level}")
            print(f"* Optimizer: {optimizer}")
            print(f"* Optimizer restarts: {n_restarts_optimizer}")
            print(f"* X-preprocessor: {preprocessing_X is not None}")
            print(f"* y-preprocessor: {preprocessing_y is not None}")
            print(f"* SVM to account for infinities: {bool(account_for_inf)}")

    @property
    def d(self):
        """Dimension of the feature space."""
        if self.bounds is None:
            return self.X_train.shape[1]
        else:
            return self.bounds.shape[0]

    @property
    def y_max(self):
        """The max. posterior value in the training set."""
        return np.max(getattr(self, "y_train", [self.minus_inf_value]))

    @property
    def n(self):
        """
        Number of points in the training set.

        This excludes infinite points if the GPR was initialized to account for them.

        To get the total number of points added to the model, both finite and infinite,
        use the property ``GaussianProcessRegressor.n_total``.
        """
        return len(getattr(self, "y_train", []))

    @property
    def n_finite(self):
        """
        Number of points in the training set. Alias of ``GaussianProcessRegressor.n``.
        """
        return self.n

    @property
    def n_total(self):
        """
        Returns the total number of points added to the model, both finite and infinite.

        Infinite points, if accounted for, are not part of the training set of the GPR.
        """
        if self.infinities_classifier:
            # The SVM usually contains all points, but maybe it hasn't been trained yet.
            # In that case, return the GPR's
            return self.infinities_classifier.n or self.n
        else:
            return self.n

    @property
    def X_train_infinite(self):
        """
        X of points in the training set which have been classified as infinite.
        """
        if self.infinities_classifier is None:
            return np.empty(shape=(0, self.d))
        return self.X_train_all[~self.infinities_classifier.y_finite]

    @property
    def y_train_infinite(self):
        """
        X of points in the training set which have been classified as infinite.
        """
        if self.infinities_classifier is None:
            return np.empty(shape=(0,))
        return self.y_train_all[~self.infinities_classifier.y_finite]

    @property
    def fitted(self):
        """Whether the GPR has been fitted at least once."""
        return self._fitted

    @property
    def last_appended(self):
        """
        Returns a copy of the last appended training points (finite/accepted or not),
        as (X, y).
        """
        if self.infinities_classifier is None:
            return self.last_appended_finite
        return (np.copy(self.X_train_all[-self.n_last_appended:]),
                np.copy(self.y_train_all[-self.n_last_appended:]))

    @property
    def last_appended_finite(self):
        """Returns a copy of the last appended GPR (finite) training points, as (X, y)."""
        return (np.copy(self.X_train[-self.n_last_appended_finite:]),
                np.copy(self.y_train[-self.n_last_appended_finite:]))

    def is_finite(self, y=None):
        """
        Returns the classification of y (target) values as finite (True) or not, by
        comparing them with the current threshold.

        Notes
        -----
        Use this method instead of the equivalent one of the 'infinities_classifier'
        attribute, since the arguments of that one may need to be transformed first.

        If calling with an argument which is not either the training set or a subset of it
        results may be inconsistent, since new values may modify the threshold.
        """
        if self.infinities_classifier is None:
            return np.full(shape=len(y), fill_value=True)
        return self.infinities_classifier.is_finite(
            self.preprocessing_y.transform(y) if y is not None else None
        )

    def predict_is_finite(self, X, validate=True):
        """
        Returns a prediction for the classification of the target value at some given
        parameters.

        Notes
        -----
        Use this method instead of the equivalent one of the 'infinities_classifier'
        attribute, since the arguments of that one may need to be transformed first.
        """
        if self.infinities_classifier is None:
            return np.full(shape=len(y), fill_value=True)
        return self.infinities_classifier.predict(
            self.preprocessing_X.transform(X), validate=validate
        )

    @property
    def abs_threshold_finite(self):
        """Current absolute threshold to decide if a target value is finite."""
        if self.infinities_classifier is None:
            return -np.info
        return self.preprocessing_y.inverse_transform(
            self.infinities_classifier.abs_threshold
        )

    def set_random_state(self, random_state):
        """
        (Re)sets the random state, including the SVM, if present.
        """
        self.random_state = random_state
        if self.infinities_classifier:
            # In the SVM case, since we have not wrapper the calls to the RNG,
            # (as we have for the GPR), we need to repackage the new numpy Generator
            # as a RandomState, which is achieved by gpry.tools.check_random_state
            self.infinities_classifier.random_state = check_random_state(
                random_state, convert_to_random_state=True)

    def append_to_data(self, X, y, noise_level=None, fit=True, simplified_fit=False,
                       hyperparameter_bounds=None):
        r"""Append newly acquired data to the GP Model and updates it.

        Here updating refers to the re-calculation of
        :math:`(K(X,X)+\sigma_n^2 I)^{-1}` which is needed for predictions. In
        most cases (except if :math:`f(x_*)=\mu(x_*)`) the hyperparameters
        :math:`\theta` need to be updated too though. Therefore the function
        offers two different methods of updating the GPR after the training data
        (``X_train, y_train``) has been updated:

           * Refit :math:`\theta` using the
             internal ``fit`` method.
           * Keep :math:`\theta` fixed and update
             :math:`(K(X,X)+\sigma_n^2 I)^{-1}` using a single matrix inversion.

        While the first method can always be applied it is considerably slower
        than the second one. Therefore it can be useful to use the second
        algorithm in cases where it is worth saving the computational expense
        such as when performing parallelized active sampling.

         .. note::

            The second method can only be used if the GPR has previously been
            trained.

        Parameters
        ----------
        X : array-like, shape = (n_samples, n_features)
            Training data to append to the model.

        y : array-like, shape = (n_samples, [n_output_dims])
            Target values to append to the data

        noise_level : array-like, shape = (n_samples, [n_output_dims])
            Uncorrelated standard deviations to add to the diagonal part of the
            covariance matrix. Needs to have the same number of entries as
            y. If None, the noise_level set in the instance is used. If you pass
            a single number the noise level will be overwritten. In this case it
            is advisable to refit the hyperparameters of the kernel.

        fit : Bool, optional (default: True)
            Whether the model is refit to new :math:`\theta`-parameters
            or just updated using the blockwise matrix-inversion lemma.
            NB: if used, the SVM classifier is always fit regardless of this arg.

        simplified_fit : Bool, optional (default: False)
            If True and ``fit=True``, hyperparameters are only optimised from the last
            optimum (otherwise, a number of optimisation processes are also started from
            samples of the hyperparameters' priors).

        Returns
        -------
        self
            Returns an instance of self.
        """
        # Check if X_train and y_train exist to see if a model has previously been fit
        if not (hasattr(self, "X_train_all_") and hasattr(self, "y_train_all_")):
            if not fit and self.verbose > 1:
                warnings.warn("No model has previously been fit to the data, "
                              "a model will be fit with X and y instead of just "
                              "updating with the same kernel hyperparameters")
            self.fit(X, y, noise_level=noise_level,
                     hyperparameter_bounds=hyperparameter_bounds)
            return self
        self.n_last_appended = y.shape[0]
        self.X_train_all = np.append(self.X_train_all, X, axis=0)
        self.y_train_all = np.append(self.y_train_all, y)
        if self.preprocessing_X is None:
            X_ = np.copy(X)
            self.X_train_all_ = np.copy(self.X_train_all)
        else:
            X_ = self.preprocessing_X.transform(X)
            self.X_train_all_ = np.append(self.X_train_all_, X_, axis=0)
        if self.preprocessing_y is None:
            y_ = np.copy(y)
            self.y_train_all_ = np.copy(self.y_train_all)
        else:
            y_ = self.preprocessing_y.transform(y)
            self.y_train_all_ = np.append(self.y_train_all_, y_, axis=0)
        # Re-fit the SVM with the new data and select only finite points for GPR
        if self.infinities_classifier is not None:
            if self.preprocessing_y is None:
                diff_threshold_ = np.copy(self.diff_threshold)
            else:
                diff_threshold_ = self.preprocessing_y.transform_noise_level(
                    self.diff_threshold
                )
            finite_last_appended = self.infinities_classifier.fit(
                self.X_train_all_, self.y_train_all_, diff_threshold_
            )[-self.n_last_appended:]
            # If all added values are infinite there's no need to refit the GP
            if not np.any(finite_last_appended):
                return self
            X, X_ = X[finite_last_appended], X_[finite_last_appended]
            y, y_ = y[finite_last_appended], y_[finite_last_appended]
            if noise_level is not None and np.iterable(noise_level):
                noise_level = noise_level[finite_last_appended]
        # Process the noise level
        self._reset_noise_level(noise_level, y.shape[0], fit)
        # Append the new data and maybe re-fit the kernel and preprocessors
        self.X_train = np.append(self.X_train, X, axis=0)
        self.y_train = np.append(self.y_train, y)
        # The number of newly added points. Used for the update_model method
        self.n_last_appended_finite = y.shape[0]
        self.newly_appended_for_inv = y.shape[0]
        if fit:
            self.fit(simplified=simplified_fit,
                     hyperparameter_bounds=hyperparameter_bounds
            )
        else:
            # Append points manually (after pre-processing) and update the kernel matrix
            self.X_train_ = np.append(self.X_train_, X_, axis=0)
            self.y_train_ = np.append(self.y_train_, y_)
            self.alpha = self.noise_level_**2.
            self._update_model()
        return self

    def _reset_noise_level(self, noise_level, n_train, fit):
        # Update noise_level, this is a bit complicated because it can be
        # a number, iterable or None. The noise level is also transformed here
        # if the data is not refit.
        if np.iterable(noise_level):
            if noise_level.shape[0] != n_train:
                raise ValueError("noise_level must be an array with same "
                                 "number of entries as y.(%d != %d)"
                                 % (noise_level.shape[0], n_train))
            elif np.iterable(self.noise_level):
                self.noise_level = np.append(self.noise_level,
                                             noise_level, axis=0)
                if not fit:
                    if self.preprocessing_y is not None:
                        noise_level_transformed = self.preprocessing_y.\
                            transform_noise_level(noise_level)
                    else:
                        noise_level_transformed = noise_level
                    self.noise_level_ = np.append(self.noise_level_,
                                                  noise_level_transformed, axis=0)
            else:
                if self.verbose > 1:
                    warnings.warn(
                        "A new noise level has been assigned to the "
                        "updated training set while the old training set has a"
                        " single scalar noise level: %s" % self.noise_level)
                noise_level = np.append(np.ones(self.y_train_.shape) *
                                        self.noise_level, noise_level, axis=0)
                self.noise_level = noise_level
                if not fit:
                    if self.preprocessing_y is not None:
                        noise_level_transformed = self.preprocessing_y.\
                            transform_noise_level(noise_level)
                    else:
                        noise_level_transformed = noise_level
                    self.noise_level_ = noise_level_transformed
        elif noise_level is None:
            if np.iterable(self.noise_level):
                raise ValueError("No value for the noise level given even "
                                 "though concrete values were given earlier. Please only "
                                 "give one scalar value or a different one for each "
                                 "training point.")
        elif isinstance(noise_level, int) or isinstance(noise_level, float):
            if not fit and self.verbose > 1:
                warnings.warn("Overwriting the noise level with a scalar "
                              "without refitting the kernel's hyperparamters. This may "
                              "cause unwanted behaviour.")
                if self.preprocessing_y is not None:
                    noise_level_transformed = \
                        self.preprocessing_y.transform_noise_level(noise_level)
                else:
                    noise_level_transformed = noise_level
                self.noise_level_ = noise_level_transformed
            self.noise_level = noise_level
        else:
            raise ValueError("noise_level needs to be an iterable, number or "
                             "None, not %s" % noise_level)

    def remove_from_data(self, position, fit=True):
        r"""
        *WARNING* This function is currently outdated and raises NotImplementedError.

        Removes data points from the GP model. Works very similarly to the
        :meth:`append_to_data` method with the only difference being that the
        position(s) of the training points to delete are given instead of
        values.

        Parameters
        ----------
        position : int or array-like, shape = (n_samples,)
            The position (index) at which to delete from the training data.
            If an array is given the data is deleted at multiple points.

        fit : Bool, optional (default: True)
            Whether the model is refit to new :math:`\theta`-parameters
            or just updated.

        Returns
        -------
        self
            Returns an instance of self.
        """
        raise NotImplementedError("This function is outdated and needs review.")
        # Legacy code below, for re-implementation
        if not (hasattr(self, "X_train_") and hasattr(self, "y_train_")):
            raise ValueError("GP model contains no points. Cannot remove "
                             "points which do not exist.")
        if np.iterable(position):
            if np.max(position) >= len(self.y_train_):
                raise ValueError("Position index is higher than length of "
                                 "training points")
        else:
            if position >= len(self.y_train_):
                raise ValueError("Position index is higher than length of "
                                 "training points")
        self.X_train_ = np.delete(self.X_train_, position, axis=0)
        self.y_train_ = np.delete(self.y_train_, position)
        self.X_train = np.delete(self.X_train, position, axis=0)
        self.y_train = np.delete(self.y_train, position)
        if np.iterable(self.noise_level):
            self.noise_level = np.delete(self.noise_level, position)
            self.noise_level_ = np.delete(self.noise_level_, position)
            self.alpha = np.delete(self.alpha, position)
        # TODO: add hyperparameter bounds
        if fit:
            self.fit(self.X_train, self.y_train)
        else:
            # Precompute quantities required for predictions which are
            # independent of actual query points
            K = self.kernel_(self.X_train_)
            K[np.diag_indices_from(K)] += self.alpha
            self._kernel_inverse(K)
        return self

    # Wrapper around log_marginal_likelihood to count the number of evaluations
    def log_marginal_likelihood(self, *args, **kwargs):
        """
        Log-marginal likelihood of the kernel hyperparameters given the training data.
        """
        self.n_eval_loglike += 1
        return super().log_marginal_likelihood(*args, **kwargs)

    def fit(self, X=None, y=None, noise_level=None, simplified=False,
            hyperparameter_bounds=None, n_restarts=None, start_from_current=True):
        r"""Optimizes the hyperparameters :math:`\theta` for the training data
        given. The algorithm used to perform the optimization is very similar
        to the one provided by Scikit-learn. The only major difference is, that
        gradient information is used in addition to the values of the
        marginalized log-likelihood.

        Parameters
        ----------
        X : array-like, shape = (n_samples, n_features), optional
            (default: None)
            Training data (full set). If None is given X_train is
            taken from the instance. None should only be passed if it is called
            from the ``append to data`` method.

        y : array-like, shape = (n_samples, [n_output_dims]), optional
            (default: None)
            Target values (full set). If None is given y_train is taken from the instance.
            None should only be passed if it is called from the
            ``append to data`` method.

        n_restarts : int, default None
            Number of restarts of the optimizer. If not defined, uses the one set at
            instantiation.

        start_from_current : bool, default: True
            Starts the fitting by optimizing from the current hyperparameters. This
            initial optimization does not count towards ``n_restarts``.

        simplified : bool, default: False
            If True, runs the optimiser only from the last optimum of the hyperparameters,
            without restarts. Shorthand for ``start_from_current=True, n_restarts=0`` (it
            overrides them if True).

        Returns
        -------
        self
        """
        if not hasattr(self, 'kernel_'):
            self.kernel_ = clone(self.kernel)
        self._rng = check_random_state(self.random_state)
        # Prepare data for which we will fit, and noise level
        # If X and y are not given, it re-fits with the current training set
        if X is None or y is None:
            # Check if X AND y are None, else raise ValueError
            if X is not None or y is not None:
                raise ValueError("X or y is None, while the other isn't. "
                                 "Either both need to be provided or both should be None")
            if noise_level is not None:
                raise ValueError("Cannot give a noise level if X and y are "
                                 "not given.")
            # Take X and y from model. We assume that all infinite values have
            # been removed by the append_to_data method
            X = np.copy(self.X_train)
            y = np.copy(self.y_train)
            noise_level = np.copy(self.noise_level)
        else:  # X and y are given as args -- takes them as the *full* training set
            self.X_train_all = np.copy(X)
            self.y_train_all = np.copy(y)
            # Set noise level
            if noise_level is None:
                if np.iterable(self.noise_level):
                    raise ValueError("If None is passed as noise level the "
                                     "internally saved noise level needs "
                                     "to be a scalar")
                noise_level = self.noise_level
            else:
                self.noise_level = noise_level
            if np.iterable(noise_level) and noise_level.shape[0] != y.shape[0]:
                raise ValueError("noise_level must be a scalar or an array "
                                 "with same number of entries as y.(%s "
                                 "!= %s)"
                                 % (noise_level.shape[0], y.shape[0]))
            # Account for infinite values
            # Notice that at the time of this call the preprocessors may not have been fit
            # This is OK: we can do a first check in the untransformed space, since the
            # SVM will be re-fitted after the preprocessors below anyway.
            if self.infinities_classifier is not None:
                if self.preprocessing_X is None or not self.preprocessing_X.fitted:
                    X_ = X
                else:
                    X_ = self.preprocessing_X.transform(X)
                if self.preprocessing_y is None or not self.preprocessing_y.fitted:
                    y_ = y
                    diff_threshold_ = self.diff_threshold
                else:
                    y_ = self.preprocessing_y.transform(y)
                    diff_threshold_ = self.preprocessing_y.transform_noise_level(
                        self.diff_threshold
                    )
                finite = self.infinities_classifier.fit(X_, y_, diff_threshold_)
                # If there are no finite values there's no point in fitting the GP.
                if np.all(~finite):
                    return self
                X = X[finite]
                y = y[finite]
                if np.iterable(noise_level):
                    noise_level = noise_level[finite]
            # Copy X and y for later use
            self.X_train = np.copy(X)
            self.y_train = np.copy(y)
        # (Re)fit preprocessors to *finite* data; store transformed data and noise level
        if self.preprocessing_X is None:
            self.X_train_ = X
            self.X_train_all_ = self.X_train_all
        else:
            self.preprocessing_X.fit(X, y)
            self.X_train_ = self.preprocessing_X.transform(X)
            self.X_train_all_ = self.preprocessing_X.transform(self.X_train_all)
        if self.preprocessing_y is None:
            self.y_train_ = y
            self.y_train_all_ = self.y_train_all
            self.noise_level_ = noise_level
        else:
            self.preprocessing_y.fit(X, y)
            self.y_train_ = self.preprocessing_y.transform(y)
            self.y_train_all_ = self.preprocessing_y.transform(self.y_train_all)
            self.noise_level_ = self.preprocessing_y.transform_noise_level(noise_level)
        self.alpha = self.noise_level_**2.
        if self.infinities_classifier is not None:
            # If preprocessors fit, the SVM needs to be refit (acts in the transf. space)
            # NB: this assumes that the finite training points are the same after
            # re-fitting the y transf., true if it preserves y-y(max) (checked above)
            if self.preprocessing_y is None:
                diff_threshold_ = self.diff_threshold
            else:
                diff_threshold_ = self.preprocessing_y.transform_noise_level(
                    self.diff_threshold
                )
            _ = self.infinities_classifier.fit(
                self.X_train_all_, self.y_train_all_, diff_threshold_
            )
        # Perform the optimization loop
        if simplified:
            start_from_current = True
            n_restarts = 0
        elif n_restarts is None:
            n_restarts = self.n_restarts_optimizer
        do_optimization = (
            self.optimizer is not None and self.kernel_.n_dims > 0 and
            (start_from_current or n_restarts)
        )
        if do_optimization:
            # Choose hyperparameters based on maximizing the log-marginal
            # likelihood (potentially starting from several initial values)
            def obj_func(theta, eval_gradient=True):
                if eval_gradient:
                    lml, grad = self.log_marginal_likelihood(
                        theta, eval_gradient=True, clone_kernel=False)
                    return -lml, -grad
                else:
                    return -self.log_marginal_likelihood(theta, clone_kernel=False)

            if hyperparameter_bounds is None:
                hyperparameter_bounds = self.kernel_.bounds
            optima = []
            # First optimize starting from theta specified in kernel
            if start_from_current:
                optima.append(
                    self._constrained_optimization(
                        obj_func, self.kernel_.theta, hyperparameter_bounds
                    )
                )
            # Additional runs are performed from log-uniform chosen initial theta
            if n_restarts > 0:
                if not np.isfinite(self.kernel_.bounds).all():
                    raise ValueError(
                        "Multiple optimizer restarts (n_restarts[_optimizer]>0) "
                        "requires that all bounds are finite.")
                for iteration in range(n_restarts):
                    theta_initial = self._rng.uniform(
                        hyperparameter_bounds[:, 0], hyperparameter_bounds[:, 1]
                    )
                    optima.append(
                        self._constrained_optimization(
                            obj_func, theta_initial, hyperparameter_bounds
                        )
                    )
            # Select result from run with minimal (negative) log-marginal likelihood
            lml_values = list(map(itemgetter(1), optima))
            self.kernel_.theta = optima[np.argmin(lml_values)][0]
            # self.kernel_._check_bounds_params()
            self.log_marginal_likelihood_value_ = -np.min(lml_values)
        else:
            self.log_marginal_likelihood_value_ = \
                self.log_marginal_likelihood(self.kernel_.theta, clone_kernel=False)
        # Precompute quantities required for predictions which are independent
        # of actual query points
        K = self.kernel_(self.X_train_)
        K[np.diag_indices_from(K)] += self.alpha
        self._kernel_inverse(K)
        # Reset newly_appended_for_inv to 0
        self.newly_appended_for_inv = 0
        self._fitted = True
        return self

    def _update_model(self):
        r"""Updates a preexisting model using a single matrix inversion.

        This method is used when a refitting of the :math:`\theta`-parameters
        is not needed. In this case only the Inverse of the Covariance matrix
        is updated. This method does not take X or y as inputs and should only
        be called from the append_to_data method.

        The X and y values used for training are taken internally from the
        instance.

        Returns
        -------
        self
        """
        # Check if a model has previously been fit to the data, i.e. that a
        # V_, X_train_ and y_train_ exist. Furthermore check, that
        # newly_appended_for_inv > 0.

        if self.newly_appended_for_inv < 1:
            raise ValueError("No new points have been appended to the model. "
                             "Please append points with the 'append_to_data'-method "
                             "before trying to update.")

        if getattr(self, "X_train_", None) is None:
            raise ValueError("X_train_ is missing. Most probably the model "
                             "hasn't been fit to the data previously.")

        if getattr(self, "y_train_", None) is None:
            raise ValueError("y_train_ is missing. Most probably the model "
                             "hasn't been fit to the data previously.")

        if getattr(self, "V_", None) is None:
            raise ValueError("V_ is missing. Most probably the model "
                             "hasn't been fit to the data previously.")

        if self.V_.shape[0] != self.y_train_.size - self.newly_appended_for_inv:
            raise ValueError("The number of added points doesn't match the "
                             "dimensions of the V_ matrix. %s != %s"
                             % (self.V_.shape[0],
                                self.y_train_.size - self.newly_appended_for_inv))

        kernel = self.kernel_(self.X_train_)
        kernel[np.diag_indices_from(kernel)] += self.alpha
        self._kernel_inverse(kernel)

        # Reset newly_appended_for_inv to 0
        self.newly_appended_for_inv = 0
        return self

    def predict(self, X, return_std=False, return_cov=False,
                return_mean_grad=False, return_std_grad=False, validate=True):
        """
        Predict output for X.

        In addition to the mean of the predictive distribution, also its
        standard deviation (return_std=True) or covariance (return_cov=True),
        the gradient of the mean and the standard-deviation with respect to X
        can be optionally provided.

        Parameters
        ----------
        X : array-like, shape = (n_samples, n_features)
            Query points where the GP is evaluated.

        return_std : bool, default: False
            If True, the standard-deviation of the predictive distribution at
            the query points is returned along with the mean.

        return_mean_grad : bool, default: False
            Whether or not to return the gradient of the mean.
            Only valid when X is a single point.

        return_std_grad : bool, default: False
            Whether or not to return the gradient of the std.
            Only valid when X is a single point.

        validate : bool, default: True
            If False, ``X`` is assumed to be correctly formatted (2-d float array, with
            points as rows and dimensions/features as columns, C-contiguous), and no
            checks are performed on it. Reduces overhead. Use only for repeated calls when
            the input is programmatically generated to be correct at each stage.

        .. note::

            Note that in contrast to the sklearn GP Regressor our implementation
            cannot return the full covariance matrix. This is to save on some
            complexity and since the full covariance cannot be calculated if
            either values are infinite or a y-preprocessor is used.

        Returns
        -------
        y_mean : array, shape = (n_samples, [n_output_dims])
            Mean of predictive distribution a query points

        y_std : array, shape = (n_samples,), optional
            Standard deviation of predictive distribution at query points.
            Only returned when return_std is True.

        y_cov : array, shape = (n_samples, n_samples), optional
            Covariance of joint predictive distribution a query points.
            Only returned when return_cov is True.

        y_mean_grad : shape = (n_samples, n_features), optional
            The gradient of the predicted mean.

        y_std_grad : shape = (n_samples, n_features), optional
            The gradient of the predicted std.
        """
        self.n_eval += len(X)

        if return_std_grad and not (return_std and return_mean_grad):
            raise ValueError(
                "Not returning std_gradient without returning "
                "the std and the mean grad.")

        if X.shape[0] != 1 and (return_mean_grad or return_std_grad):
            raise ValueError("Mean grad and std grad not implemented \
                for n_samples > 1")

        if validate and (self.kernel is None or self.kernel.requires_vector_input):
            X = check_array(X, ensure_2d=True, dtype="numeric")
        elif validate:
            X = check_array(X, ensure_2d=False, dtype=None)

        if not hasattr(self, "X_train_"):  # Not fit; predict based on GP prior
            # we assume that since the GP has not been fit to data the SVM can
            # be ignored
            y_mean = np.zeros(X.shape[0])
            if return_std:
                y_var = self.kernel.diag(X)
                y_std = np.sqrt(y_var)
                if not return_mean_grad and not return_std_grad:
                    return y_mean, y_std
            if return_mean_grad:
                mean_grad = np.zeros_like(X)
                if return_std:
                    if return_std_grad:
                        std_grad = np.zeros_like(X)
                        return y_mean, y_std, mean_grad, std_grad
                    else:
                        return y_mean, y_std, mean_grad
                else:
                    return y_mean, mean_grad
            else:
                return y_mean

        # First check if the SVM says that the value should be -inf
        if self.infinities_classifier is not None:
            # Every variable that ends in _full is the full (including infinite)
            # values
            X = np.copy(X)  # copy since we might change it
            n_samples = X.shape[0]
            n_dims = X.shape[1]
            # Initialize the full arrays for filling them later with infinite
            # and non-infinite values
            y_mean_full = np.ones(n_samples)
            y_std_full = np.zeros(n_samples)  # std is zero when mu is -inf
            grad_mean_full = np.ones((n_samples, n_dims))
            grad_std_full = np.zeros((n_samples, n_dims))
            X_ = X if self.preprocessing_X is None else self.preprocessing_X.transform(X)
            finite = self.infinities_classifier.predict(X_, validate=validate)
            # If all values are infinite there's no point in running the
            # prediction through the GP
            if np.all(~finite):
                y_mean = y_mean_full * self.minus_inf_value
                if return_std:
                    y_std = np.zeros(n_samples)
                    if not return_mean_grad and not return_std_grad:
                        return y_mean, y_std
                if return_mean_grad:
                    grad_mean = np.ones((n_samples, n_dims)) * self.inf_value
                    if return_std:
                        if return_std_grad:
                            grad_std = np.zeros((n_samples, n_dims))
                            return y_mean, y_std, grad_mean, grad_std
                        else:
                            return y_mean, y_std, grad_mean
                    else:
                        return y_mean, grad_mean
                return y_mean

            y_mean_full[~finite] = self.minus_inf_value  # Set infinite values
            grad_mean_full[~finite] = self.inf_value  # the grad of inf values is +inf
            X = X[finite]  # only predict the finite samples

        if self.preprocessing_X is not None:
            X = self.preprocessing_X.transform(X)

        # Predict based on GP posterior
        K_trans = self.kernel_(X, self.X_train_)
        y_mean = K_trans.dot(self.alpha_)    # Line 4 (y_mean = f_star)
        # Undo normalization
        if self.preprocessing_y is not None:
            y_mean = self.preprocessing_y.inverse_transform(y_mean)
        # Put together with SVM predictions
        if self.infinities_classifier is not None:
            y_mean_full[finite] = y_mean
            y_mean = y_mean_full

        if return_std:
            M = tri_mul(1., self.V_, K_trans.T, lower=True)

            # Compute variance of predictive distribution
            y_var = self.kernel_.diag(X)
            y_var -= np.einsum("ji,ji->i", M, M, optimize=True)
            # np.einsum("ij,ij->i", np.dot(K_trans, K_inv), K_trans)
            # np.einsum("ki,kj,ij->k", K_trans, K_trans, K_inv)

            # Check if any of the variances is negative because of
            # numerical issues. If yes: set the variance to 0.
            y_var_negative = y_var < 0
            if np.any(y_var_negative):
                if self.verbose > 4:
                    warnings.warn("Predicted variances smaller than 0. "
                                  "Setting those variances to 0.")
                y_var[y_var_negative] = 0.0
            y_std = np.sqrt(y_var)

            y_std_untransformed = np.copy(y_std)

            # Undo normalization
            if self.preprocessing_y is not None:
                y_std = self.preprocessing_y.\
                    inverse_transform_noise_level(y_std)
            # Add infinite values
            if self.infinities_classifier is not None:
                y_std_full[finite] = y_std
                y_std = y_std_full

            if not return_mean_grad and not return_std_grad:
                return y_mean, y_std

        if return_mean_grad:
            grad = self.kernel_.gradient_x(X[0], self.X_train_)
            grad_mean = np.dot(grad.T, self.alpha_)
            # Undo normalization
            if self.preprocessing_y is not None:
                grad_mean = self.preprocessing_y.\
                    inverse_transform_noise_level(grad_mean)
            # Include infinite values
            if self.infinities_classifier is not None:
                grad_mean_full[finite] = grad_mean
                grad_mean = grad_mean_full
            if return_std_grad:
                grad_std = np.zeros(X.shape[1])
                if not np.allclose(y_std, grad_std):
                    # TODO: This can be made much more efficient, but I don't think it's used currently
                    grad_std = -np.dot(K_trans,
                                       np.dot(self.V_.T.dot(self.V_), grad))[0] \
                        / y_std_untransformed
                    # Undo normalization
                    if self.preprocessing_y is not None:
                        # Apply inverse transformation twice
                        grad_std = self.preprocessing_y.\
                            inverse_transform_noise_level(grad_std)
                        grad_std = self.preprocessing_y.\
                            inverse_transform_noise_level(grad_std)
                    # Include infinite values
                    if self.infinities_classifier is not None:
                        grad_std_full[finite] = grad_std
                        grad_std = grad_std_full
                return y_mean, y_std, grad_mean, grad_std

            if return_std:
                return y_mean, y_std, grad_mean
            else:
                return y_mean, grad_mean

        return y_mean

    def predict_std(self, X, validate=True):
        """
        Predict output standart deviation for X.

        Parameters
        ----------
        X : array-like, shape = (n_samples, n_features)
            Query points where the GP is evaluated.

        validate : bool, default: True
            If False, ``X`` is assumed to be correctly formatted (2-d float array, with
            points as rows and dimensions/features as columns, C-contiguous), and no
            checks are performed on it. Reduces overhead. Use only for repeated calls when
            the input is programmatically generated to be correct at each stage.

        Returns
        -------
        y_std : array, shape = (n_samples,), optional
            Standard deviation of predictive distribution at query points.
            Only returned when return_std is True.
        """
        self.n_eval += len(X)

        if validate and (self.kernel is None or self.kernel.requires_vector_input):
            X = check_array(X, ensure_2d=True, dtype="numeric")
        elif validate:
            X = check_array(X, ensure_2d=False, dtype=None)

        if not hasattr(self, "X_train_"):  # Not fit; predict based on GP prior
            # we assume that since the GP has not been fit to data the SVM can be ignored
            return np.sqrt(self.kernel.diag(X))

        # First check if the SVM says that the value should be -inf
        if self.infinities_classifier is not None:
            # Every variable that ends in _full is the full (including infinite) values
            X = np.copy(X)  # copy since we might change it
            n_samples = X.shape[0]
            # Initialize the full arrays for filling them later with infinite
            # and non-infinite values
            y_std_full = np.zeros(n_samples)  # std is zero when mu is -inf
            X_ = X if self.preprocessing_X is None else self.preprocessing_X.transform(X)
            finite = self.infinities_classifier.predict(X_, validate=validate)
            # If all values are infinite there's no point in running the
            # prediction through the GP
            if np.all(~finite):
                return np.zeros(n_samples)
            X = X[finite]  # only predict the finite samples

        if self.preprocessing_X is not None:
            X = self.preprocessing_X.transform(X)

        # Predict based on GP posterior
        K_trans = self.kernel_(X, self.X_train_)
        M = tri_mul(1., self.V_, K_trans.T, lower=True)
        # Compute variance of predictive distribution
        y_var = self.kernel_.diag(X)
        y_var -= np.einsum("ji,ji->i", M, M, optimize=True)
        # np.einsum("ij,ij->i", np.dot(K_trans, K_inv), K_trans)
        # np.einsum("ki,kj,ij->k", K_trans, K_trans, K_inv)
        # Check if any of the variances is negative because of
        # numerical issues. If yes: set the variance to 0.
        y_var_negative = y_var < 0
        if np.any(y_var_negative):
            if self.verbose > 4:
                warnings.warn("Predicted variances smaller than 0. "
                              "Setting those variances to 0.")
            y_var[y_var_negative] = 0.0
        y_std = np.sqrt(y_var)
        # Undo normalization
        if self.preprocessing_y is not None:
            y_std = self.preprocessing_y.\
                inverse_transform_noise_level(y_std)
        # Add infinite values
        if self.infinities_classifier is not None:
            y_std_full[finite] = y_std
            y_std = y_std_full
        return y_std

    def __deepcopy__(self, memo):
        """
        Overwrites the internal deepcopy method of the class in order to
        also copy instance variables which are not defined in the init.
        """
        # Initialize the stuff specified in init
        c = GaussianProcessRegressor(
            kernel=self.kernel,
            noise_level=self.noise_level,
            optimizer=self.optimizer,
            n_restarts_optimizer=self.n_restarts_optimizer,
            preprocessing_X=self.preprocessing_X,
            preprocessing_y=self.preprocessing_y,
            bounds=self.bounds,
            random_state=self.random_state)

        # Remember number of evaluations
        if hasattr(self, "n_eval"):
            c.n_eval = self.n_eval
        if hasattr(self, "n_eval_loglike"):
            c.n_eval_loglike = self.n_eval_loglike
        # Initialize the X_train and y_train part
        if hasattr(self, "X_train"):
            c.X_train = np.copy(self.X_train)
        if hasattr(self, "y_train"):
            c.y_train = np.copy(self.y_train)
        if hasattr(self, "X_train_"):
            c.X_train_ = np.copy(self.X_train_)
        if hasattr(self, "y_train_"):
            c.y_train_ = np.copy(self.y_train_)
        if hasattr(self, "X_train_all"):
            c.X_train_all = np.copy(self.X_train_all)
        if hasattr(self, "y_train_all"):
            c.y_train_all = np.copy(self.y_train_all)
        if hasattr(self, "X_train_all_"):
            c.X_train_all_ = np.copy(self.X_train_all_)
        if hasattr(self, "y_train_all_"):
            c.y_train_all_ = np.copy(self.y_train_all_)
        # Initialize noise levels
        if hasattr(self, "noise_level"):
            c.noise_level = self.noise_level
        if hasattr(self, "noise_level_"):
            c.noise_level_ = self.noise_level_
        if hasattr(self, "alpha"):
            c.alpha = self.alpha
        # Initialize kernel and inverse kernel
        if hasattr(self, "V_"):
            c.V_ = np.copy(self.V_)
        if hasattr(self, "L_"):
            c.L_ = np.copy(self.L_)
        if hasattr(self, "alpha_"):
            c.alpha_ = np.copy(self.alpha_)
        if hasattr(self, "kernel_"):
            c.kernel_ = deepcopy(self.kernel_)
        # Copy the right SVM
        if hasattr(self, "infinities_classifier"):
            c.infinities_classifier = deepcopy(self.infinities_classifier)
        if hasattr(self, "diff_threshold"):
            c.diff_threshold = deepcopy(self.diff_threshold)
        if hasattr(self, "inf_value"):
            c.inf_value = deepcopy(self.inf_value)
        if hasattr(self, "minus_inf_value"):
            c.minus_inf_value = deepcopy(self.minus_inf_value)
        # Remember number of last appended points
        if hasattr(self, "n_last_appended"):
            c.n_last_appended = self.n_last_appended
        if hasattr(self, "n_last_appended_finite"):
            c.n_last_appended_finite = self.n_last_appended_finite
        if hasattr(self, "newly_appended_for_inv"):
            c.newly_appended_for_inv = self.newly_appended_for_inv
        # Remember if it has been fit to data.
        if hasattr(self, "_fitted"):
            c._fitted = self._fitted
        return c

    def _constrained_optimization(self, obj_func, initial_theta, bounds):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            if self.optimizer == "fmin_l_bfgs_b":
                opt_res = scipy.optimize.minimize(
                    obj_func, initial_theta, method="L-BFGS-B", jac=True,
                    bounds=bounds)
                # Temporarily disabled bc incompatibility between current sklearn+scipy
                # if self.verbose > 1:
                #     _check_optimize_result("lbfgs", opt_res)
                theta_opt, func_min = opt_res.x, opt_res.fun
            elif callable(self.optimizer):
                theta_opt, func_min = \
                    self.optimizer(obj_func, initial_theta, bounds=bounds)
            else:
                raise ValueError("Unknown optimizer %s." % self.optimizer)
        return theta_opt, func_min

    def _kernel_inverse(self, kernel):
        """ Compute inverse of the kernel and store relevant quantities"""
        try:
            self.L_ = cholesky(kernel, lower=True)
            self.V_ = solve_triangular(self.L_, np.eye(self.L_.shape[0]),lower=True)
        except np.linalg.LinAlgError as exc:
            exc.args = ("The kernel, %s, is not returning a "
                        "positive definite matrix. Try gradually "
                        "increasing the 'noise_level' parameter of your "
                        "GaussianProcessRegressor estimator."
                        % self.kernel_) + exc.args
            raise
        self.alpha_ = cho_solve((self.L_, True), self.y_train_)

    @staticmethod
    def compute_threshold_given_sigma(n_sigma, n_dimensions):
        r"""
        Computes threshold value given a number of :math:`\sigma` away from the maximum,
        assuming a :math:`\chi^2` distribution.
        """
        return nstd_of_1d_nstd(n_sigma, n_dimensions)
        return 0.5 * nstd_of_1d_nstd(n_sigma, n_dimensions) ** 2

    # TO BE DEPRECATED
    @property
    def account_for_inf(self):
        warnings.warn(
            "This attr has been renamed to 'infinities_classifier'. "
            "This will raise an error soon."
        )
        return self.infinities_classifier
