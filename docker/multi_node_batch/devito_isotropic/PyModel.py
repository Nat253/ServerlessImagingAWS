import numpy as np
import os

from devito import Grid, Function, Constant, SubDomain
from devito.logger import error


__all__ = ['Model']

class PhysicalDomain(SubDomain):

    name = 'phydomain'

    def __init__(self, nbpml):
        super(PhysicalDomain, self).__init__()
        self.nbpml = nbpml

    def define(self, dimensions):
        return {d: ('middle', self.nbpml, self.nbpml) for d in dimensions}


def initialize_damp(damp, nbpml, spacing, mask=False):
    """Initialise damping field with an absorbing PML layer.
    :param damp: The :class:`Function` for the damping field.
    :param nbpml: Number of points in the damping layer.
    :param spacing: Grid spacing coefficient.
    :param mask: whether the dampening is a mask or layer.
        mask => 1 inside the domain and decreases in the layer
        not mask => 0 inside the domain and increase in the layer
    """

    phy_shape = damp.grid.subdomains['phydomain'].shape
    data = np.ones(phy_shape) if mask else np.zeros(phy_shape)

    pad_widths = [(nbpml, nbpml) for i in range(damp.ndim)]
    data = np.pad(data, pad_widths, 'edge')

    dampcoeff = 1.5 * np.log(1.0 / 0.001) / (40.)

    assert all(damp._offset_domain[0] == i for i in damp._offset_domain)

    for i in range(damp.ndim):
        for j in range(nbpml):
            # Dampening coefficient
            pos = np.abs((nbpml - j + 1) / float(nbpml))
            val = dampcoeff * (pos - np.sin(2*np.pi*pos)/(2*np.pi))
            if mask:
                val = -val
            # : slices
            all_ind = [slice(0, d) for d in data.shape]
            # Left slice for dampening for dimension i
            all_ind[i] = slice(j, j+1)
            data[tuple(all_ind)] += val/spacing[i]
            # right slice for dampening for dimension i
            all_ind[i] = slice(data.shape[i]-j, data.shape[i]-j+1)
            data[tuple(all_ind)] += val/spacing[i]

    initialize_function(damp, data, 0)


def initialize_function(function, data, nbpml, pad_mode='edge'):
    """Initialize a :class:`Function` with the given ``data``. ``data``
    does *not* include the PML layers for the absorbing boundary conditions;
    these are added via padding by this function.
    :param function: The :class:`Function` to be initialised with some data.
    :param data: The data array used for initialisation.
    :param nbpml: Number of PML layers for boundary damping.
    :param pad_mode: A string or a suitable padding function as explained in
                     :func:`numpy.pad`.
    """
    pad_widths = [(nbpml + i.left, nbpml + i.right) for i in function._size_halo]
    data = np.pad(data, pad_widths, pad_mode)
    function.data_with_halo[:] = data

class Model(object):
    """The physical model used in seismic inversion processes.
    :param origin: Origin of the model in m as a tuple in (x,y,z) order
    :param spacing: Grid size in m as a Tuple in (x,y,z) order
    :param shape: Number of grid points size in (x,y,z) order
    :param vp: Velocity in km/s
    :param nbpml: The number of PML layers for boundary damping
    :param dm: Model perturbation in s^2/km^2
    The :class:`Model` provides two symbolic data objects for the
    creation of seismic wave propagation operators:
    :param m: The square slowness of the wave
    :param damp: The damping field for absorbing boundarycondition
    """
    def __init__(self, origin, spacing, shape, vp, rho=1, nbpml=40, dtype=np.float32, dm=None,
                 epsilon=None, delta=None, theta=None, phi=None, space_order=8, in_dim=None):

        self.shape = shape
        self.nbpml = int(nbpml)
        self.origin = tuple([dtype(o) for o in origin])
        self._is_tti = False
        # Origin of the computational domain with PML to inject/interpolate
        # at the correct index
        origin_pml = tuple([dtype(o - s*nbpml) for o, s in zip(origin, spacing)])
        phydomain = PhysicalDomain(self.nbpml)
        shape_pml = np.array(shape) + 2 * self.nbpml
        # Physical extent is calculated per cell, so shape - 1
        extent = tuple(np.array(spacing) * (shape_pml - 1))
        self.grid = Grid(extent=extent, shape=shape_pml, origin=origin_pml, dtype=dtype,
                         subdomains=phydomain, dimensions=in_dim)

        # Create square slowness of the wave as symbol `m`
        if isinstance(vp, np.ndarray):
            self.m = Function(name="m", grid=self.grid, space_order=space_order)
        else:
            self.m = 1/vp**2

        if isinstance(rho, np.ndarray):
            self.rho = Function(name="rho", grid=self.grid, space_order=space_order)
            initialize_function(self.rho, rho, self.nbpml)
        else:
            self.rho = rho

        # Set model velocity, which will also set `m`
        self.vp = vp

        # Create dampening field as symbol `damp`
        self.damp = Function(name="damp", grid=self.grid)
        initialize_damp(self.damp, self.nbpml, self.spacing, mask=True)

        # Additional parameter fields for TTI operators
        self.scale = 1.

        if dm is not None:
            self.dm = Function(name="dm", grid=self.grid, space_order=space_order)
            initialize_function(self.dm, dm, self.nbpml)
        else:
            self.dm = 1

        if epsilon is not None:
            self._is_tti = True
            if isinstance(epsilon, np.ndarray):
                self.epsilon = Function(name="epsilon", grid=self.grid, space_order=space_order)
                initialize_function(self.epsilon, 1 + 2 * epsilon, self.nbpml)
                # Maximum velocity is scale*max(vp) if epsilon > 0
                if np.max(self.epsilon.data) > 0:
                    self.scale = np.sqrt(np.max(self.epsilon.data))
            else:
                self.epsilon = 1 + 2 * epsilon
                self.scale = np.sqrt(self.epsilon)
        else:
            self.epsilon = 1.0
            self.scale = 1.0

        if delta is not None:
            self._is_tti = True
            if isinstance(delta, np.ndarray):
                self.delta = Function(name="delta", grid=self.grid, space_order=space_order)
                initialize_function(self.delta, np.sqrt(1 + 2 * delta), self.nbpml)
            else:
                self.delta = np.sqrt(1 + 2 * delta)
        else:
            self.delta = 1.0

        if theta is not None:
            self._is_tti = True
            if isinstance(theta, np.ndarray):
                self.theta = Function(name="theta", grid=self.grid, space_order=space_order)
                initialize_function(self.theta, theta, self.nbpml)
            else:
                self.theta = theta
        else:
            self.theta = 0.0

        if phi is not None:
            self._is_tti = True
            if isinstance(phi, np.ndarray):
                self.phi = Function(name="phi", grid=self.grid, space_order=space_order)
                initialize_function(self.phi, phi, self.nbpml)
            else:
                self.phi = phi
        else:
            self.phi = 0.0


    @property
    def is_tti(self):
        return self._is_tti

    @property
    def dim(self):
        """
        Spatial dimension of the problem and model domain.
        """
        return self.grid.dim

    @property
    def spacing(self):
        """
        Grid spacing for all fields in the physical model.
        """
        return self.grid.spacing

    @property
    def spacing_map(self):
        """
        Map between spacing symbols and their values for each :class:`SpaceDimension`
        """
        subs = self.grid.spacing_map
        subs[self.grid.time_dim.spacing] = self.critical_dt
        return subs

    @property
    def dtype(self):
        """
        Data type for all assocaited data objects.
        """
        return self.grid.dtype

    @property
    def shape_domain(self):
        """Computational shape of the model domain, with PML layers"""
        return self.grid.shape

    @property
    def domain_size(self):
        """
        Physical size of the domain as determined by shape and spacing
        """
        return tuple((d-1) * s for d, s in zip(self.shape, self.spacing))

    @property
    def critical_dt(self):
        """Critical computational time step value from the CFL condition."""
        # For a fixed time order this number goes down as the space order increases.
        #
        # The CFL condtion is then given by
        # dt <= coeff * h / (max(velocity))
        coeff = 0.38 if len(self.shape) == 3 else 0.42
        dt = self.dtype(coeff * np.min(self.spacing) / (self.scale*np.max(self.vp)))
        return self.dtype(.001 * int(1000 * dt))

    @property
    def vp(self):
        """:class:`numpy.ndarray` holding the model velocity in km/s.
        .. note::
        Updating the velocity field also updates the square slowness
        ``self.m``. However, only ``self.m`` should be used in seismic
        operators, since it is of type :class:`Function`.
        """
        return self._vp

    @vp.setter
    def vp(self, vp):
        """Set a new velocity model and update square slowness
        :param vp : new velocity in km/s
        """
        self._vp = vp

        # Update the square slowness according to new value
        if isinstance(vp, np.ndarray):
            initialize_function(self.m, 1 / (self.vp * self.vp), self.nbpml)
        else:
            self.m.data = 1 / vp**2
