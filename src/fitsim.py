""" Fitting simulation base class. """

import abc
import os
import subprocess
import shutil
import numpy as np
from nifty import col,printcool_dictionary, link_dir_contents
from finite_difference import fdwrap_G, fdwrap_H, f1d2p, f12d3p
from optimizer import Counter

class FittingSimulation(object):
    
    """
    Base class for all fitting simulations.
    
    In ForceBalance a 'fitting simulation' is defined as a simulation
    which computes a quantity that we can compare to a reference.  The
    force field parameters are tuned to reproduce the reference value as
    closely as possible.
    
    The 'computable quantities' may include energies and forces where the
    reference values come from QM calculations (energy and force matching),
    energies from an EDA analysis (Maybe in the future, FDA?), molecular
    properties (like polarizability, refractive indices, multipole moments
    or vibrational frequencies), relative entropies, and bulk properties.
    Single-molecule or bulk properties can even come from the experiment!
    
    The central idea in ForceBalance is that each quantity makes a
    contribution to the overall objective function.  So we can build force
    fields that fit several quantities at once, rather than putting all of
    our chips behind energy and force matching.  In the future
    ForceBalance may even include multiobjective optimization into the
    optimizer.
    
    The optimization is done by way of minimizing an 'objective
    function', which is comprised of squared differences between the
    computed and reference values.  These differences are not computed
    in this file, but rather in subclasses that use FittingSimulation
    as a base class.  Thus, the contents of FittingSimulation itself
    are meant to be as general as possible, because the pertinent
    variables apply to all types of fitting simulations.

    An important node: FittingSimulation requires that all subclasses
    have a method get(self,mvals,AGrad=False,AHess=False)
    that does the following:
    
    Inputs: 
    mvals        = The parameter vector, which modifies the force field
    (Note to self: We include mvals with each FitSim because we can create
    copies of the force field and do finite difference derivatives)
    AGrad, AHess = Boolean switches for computing analytic gradients and Hessians

    Outputs:
    Answer       = {'X': Number, 'G': numpy.array(np), 'H': numpy.array((np,np)) }
    'X'          = The objective function itself
    'G'          = The gradient, elements not computed analytically are zero
    'H'          = The Hessian, elements not computed analytically are zero

    This is the only global requirement of a FittingSimulation.
    Obviously 'get' itself is not defined here, because its
    calculation will depend entirely on specifically which simulation
    we wish to run.  However, this should give us a unified framework
    which will faciliate rapid implementation of FittingSimulations.

    Future work:
    Robert suggested that I could enable automatic detection of which
    parameters need to be computed by finite difference.  Not a bad idea. :)

    """

    __metaclass__ = abc.ABCMeta
    
    def __init__(self,options,sim_opts,forcefield):
        """
        Instantiation of a fitting simulation.

        All options here are intended to be usable by every
        conceivable type of fitting simulation (in other words, only
        add content here if it's widely applicable.)

        If we want to add attributes that are more specific
        (i.e. a set of reference forces for force matching), they
        are added in the subclass ForceEnergyMatch that subclasses
        FittingSimulation.

        """
        
        #======================================#
        # Options that are given by the parser #
        #======================================#
        ## Root directory of the whole project
        self.root        = options['root']
        ## Name of the fitting simulation
        self.name        = sim_opts['name']
        ## Type of fitting simulation
        self.simtype     = sim_opts['simtype']
        ## Relative weight of the fitting simulation
        self.weight      = sim_opts['weight']
        ## Switch for finite difference gradients
        self.fdgrad      = sim_opts['fdgrad']
        ## Switch for finite difference Hessians
        self.fdhess      = sim_opts['fdhess']
        ## Switch for FD gradients + Hessian diagonals
        self.fdhessdiag  = sim_opts['fdhessdiag']
        ## Parameter types that trigger FD gradient elements
        self.fd1_pids    = [i.upper() for i in sim_opts['fd_ptypes']]
        ## Parameter types that trigger FD Hessian elements
        self.fd2_pids    = [i.upper() for i in sim_opts['fd_ptypes']]
        ## Finite difference step size
        self.h           = options['finite_difference_h']
        ## Work Queue Port (The specific simulation itself may or may not actually use this.)
        self.wq_port     = sim_opts['wq_port']
        ## Manual override: bypass the parameter transformation and use
        ## physical parameters directly.  For power users only! :)
        self.usepvals    = sim_opts['use_pvals']
        ## Whether to make backup files
        self.backup      = options['backup']
                                                                 
        #======================================#
        #     Variables which are set here     #
        #======================================#
        ## Relative directory of fitting simulation
        self.simdir      = os.path.join('simulations',self.name)
        ## Temporary (working) directory; it is temp/(simulation_name)
        ## Used for storing temporary variables that don't change through the course of the optimization
        self.tempdir     = os.path.join('temp',self.name)
        ## The directory in which the simulation is running - this can be updated.
        self.rundir      = self.tempdir
        ## Need the forcefield (here for now)
        self.FF          = forcefield
        ## Counts how often the objective function was computed
        self.xct         = 0
        ## Counts how often the gradient was computed
        self.gct         = 0
        ## Counts how often the Hessian was computed
        self.hct         = 0
        
        #======================================#
        #          UNDER DEVELOPMENT           #
        #======================================#
        # Create a new temp directory.
        self.refresh_temp_directory()

        # Print the options for this simulation to the terminal.
        printcool_dictionary(sim_opts,"Setup for fitting simulation %s :" % self.name)

    def get_X(self,mvals=None):
        """Computes the objective function contribution without any parametric derivatives"""
        Ans = self.sget(mvals,0,0)
        self.xct += 1
        if Ans['X'] != Ans['X']:
            return {'X':1e10, 'G':np.zeros(self.FF.np), 'H':np.zeros((self.FF.np,self.FF.np))}
        return Ans

    def get_G(self,mvals=None):
        """Computes the objective function contribution and its gradient.

        First the low-level 'get' method is called with the analytic gradient
        switch turned on.  Then we loop through the fd1_pids and compute
        the corresponding elements of the gradient by finite difference,
        if the 'fdgrad' switch is turned on.  Alternately we can compute
        the gradient elements and diagonal Hessian elements at the same time
        using central difference if 'fdhessdiag' is turned on.
        """
        Ans = self.sget(mvals,1,0)
        for i in range(self.FF.np):
            if any([j in self.FF.plist[i] for j in self.fd1_pids]) or 'ALL' in self.fd1_pids:
                if self.fdhessdiag:
                    Ans['G'][i], Ans['H'][i,i] = f12d3p(fdwrap_G(self,mvals,i),self.h,f0 = Ans['X'])
                elif self.fdgrad:
                    Ans['G'][i] = f1d2p(fdwrap_G(self,mvals,i),self.h,f0 = Ans['X'])
        self.gct += 1
        return Ans

    def get_H(self,mvals=None):
        """Computes the objective function contribution and its gradient / Hessian.

        First the low-level 'get' method is called with the analytic gradient
        and Hessian both turned on.  Then we loop through the fd1_pids and compute
        the corresponding elements of the gradient by finite difference,
        if the 'fdgrad' switch is turned on.

        This is followed by looping through the fd2_pids and computing the corresponding
        Hessian elements by finite difference.  Forward finite difference is used
        throughout for the sake of speed.
        """
        Ans = self.sget(mvals,1,1)
        if self.fdhess:
            for i in range(self.FF.np):
                if any([j in self.FF.plist[i] for j in self.fd1_pids]) or 'ALL' in self.fd1_pids:
                    Ans['G'][i] = f1d2p(fdwrap_G(self,mvals,i),self.h,f0 = Ans['X'])
            for i in range(self.FF.np):
                if any([j in self.FF.plist[i] for j in self.fd2_pids]) or 'ALL' in self.fd2_pids:
                    FDSlice = f1d2p(fdwrap_H(self,mvals,i),self.h,f0 = Ans['G'])
                    Ans['H'][i,:] = FDSlice
                    Ans['H'][:,i] = col(FDSlice.T)
        elif self.fdhessdiag:
            for i in range(self.FF.np):
                if any([j in self.FF.plist[i] for j in self.fd2_pids]) or 'ALL' in self.fd2_pids:
                    Ans['G'][i], Ans['H'][i,i] = f12d3p(fdwrap_G(self,mvals,i),self.h)
        self.hct += 1
        return Ans
    
    def link_from_tempdir(self,absdestdir):
        link_dir_contents(os.path.join(self.root,self.tempdir), absdestdir)

    def refresh_temp_directory(self):
        """ Back up the temporary directory if desired, delete it
        and then create a new one."""
        cwd = os.getcwd()
        abstempdir = os.path.join(self.root,self.tempdir)
        if self.backup:
            if not os.path.exists(os.path.join(self.root,'backups')):
                os.makedirs(os.path.join(self.root,'backups'))
            if os.path.exists(abstempdir):
                os.chdir(os.path.join(self.root,"temp"))
                FileCount = 0
                while True:
                    CandFile = os.path.join(self.root,'backups',"%s.%i.tar.bz2" % (self.name,FileCount))
                    if os.path.exists(CandFile):
                        FileCount += 1
                    else:
                        # I could use the tarfile module here
                        print "Backing up:", self.tempdir, 'to:', "backups/%s.%i.tar.bz2" % (self.name,FileCount)
                        subprocess.call(["tar","cjf",CandFile,self.name,"--remove-files"])
                        break
                os.chdir(cwd)
        # Delete the temporary directory
        shutil.rmtree(abstempdir,ignore_errors=True)
        # Create a new temporary directory from scratch
        os.makedirs(abstempdir)

    @abc.abstractmethod
    def get(self,mvals,AGrad=False,AHess=False):

        """ 
        
        Every fitting simulation must be able to return a contribution
        to the objective function - however, this must be implemented
        in the specific subclass.  See forceenergymatch for an
        example.

        """
        
        raise NotImplementedError('The get method is not implemented in the FittingSimulation base class')

    def sget(self, mvals, AGrad=False, AHess=False, customdir=None):
        """ 

        Stages the directory for the fitting simulation, and then calls 'get'.
        The 'get' method should not worry about the directory that it's running in.
        
        """
        ## Directory of the current iteration; if not None, then the simulation runs under
        ## temp/simulation_name/iteration_number
        ## The 'customdir' is customizable and can go below anything
        cwd = os.getcwd()
        
        absgetdir = os.path.join(self.root,self.tempdir)
        if Counter() is not None:
            # Not expecting more than ten thousand iterations
            iterdir = "iter_%04i" % Counter()
            absgetdir = os.path.join(absgetdir,iterdir)
        if customdir is not None:
            absgetdir = os.path.join(absgetdir,customdir)

        if not os.path.exists(absgetdir):
            os.makedirs(absgetdir)
        os.chdir(absgetdir)
        self.link_from_tempdir(absgetdir)
        self.rundir = absgetdir.replace(self.root+'/','')

        Answer = self.get(mvals, AGrad, AHess)
        os.chdir(cwd)
        
        return Answer