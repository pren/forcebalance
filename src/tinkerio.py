""" @package forcebalance.tinkerio TINKER input/output.

This serves as a good template for writing future force matching I/O
modules for other programs because it's so simple.

@author Lee-Ping Wang
@date 01/2012
"""

import os, shutil
from re import match, sub
from forcebalance.nifty import *
from forcebalance.nifty import _exec
import numpy as np
import networkx as nx
from copy import deepcopy
from forcebalance import BaseReader
from subprocess import Popen, PIPE
from forcebalance.engine import Engine
from forcebalance.abinitio import AbInitio
from forcebalance.vibration import Vibration
from forcebalance.moments import Moments
from forcebalance.liquid import Liquid
from forcebalance.molecule import Molecule
from forcebalance.binding import BindingEnergy
from forcebalance.interaction import Interaction
from forcebalance.finite_difference import in_fd
from collections import OrderedDict
from forcebalance.optimizer import GoodStep

# All TINKER force field parameter types, which should eventually go into pdict
# at some point (for full compatibility).
allp = ['atom', 'vdw', 'vdw14', 'vdwpr', 'hbond', 'bond', 'bond5', 'bond4',
        'bond3', 'electneg', 'angle', 'angle5', 'angle4', 'angle3', 'anglef',
        'strbnd', 'ureybrad', 'angang', 'opbend', 'opdist', 'improper', 'imptors',
        'torsion', 'torsion5', 'torsion4', 'pitors', 'strtors', 'tortors', 'charge',
        'dipole', 'dipole5', 'dipole4', 'dipole3', 'multipole', 'polarize', 'piatom',
        'pibond', 'pibond5', 'pibond4', 'metal', 'biotype', 'mmffvdw', 'mmffbond',
        'mmffbonder', 'mmffangle', 'mmffstrbnd', 'mmffopbend', 'mmfftorsion', 'mmffbci',
        'mmffpbci', 'mmffequiv', 'mmffdefstbn', 'mmffcovrad', 'mmffprop', 'mmffarom']

# All possible output from analyze's energy component breakdown.
eckeys = ['Angle-Angle', 'Angle Bending', 'Atomic Multipoles', 'Bond Stretching', 'Charge-Charge', 
          'Charge-Dipole', 'Dipole-Dipole', 'Extra Energy Terms', 'Geometric Restraints', 'Implicit Solvation', 
          'Improper Dihedral', 'Improper Torsion', 'Metal Ligand Field', 'Out-of-Plane Bend', 'Out-of-Plane Distance', 
          'Pi-Orbital Torsion', 'Polarization', 'Reaction Field', 'Stretch-Bend', 'Stretch-Torsion', 
          'Torsional Angle', 'Torsion-Torsion', 'Urey-Bradley', 'Van der Waals']

from forcebalance.output import getLogger
logger = getLogger(__name__)

pdict = {'VDW'          : {'Atom':[1], 2:'S',3:'T',4:'D'}, # Van der Waals distance, well depth, distance from bonded neighbor?
         'BOND'         : {'Atom':[1,2], 3:'K',4:'B'},     # Bond force constant and equilibrium distance (Angstrom)
         'ANGLE'        : {'Atom':[1,2,3], 4:'K',5:'B'},   # Angle force constant and equilibrium angle
         'UREYBRAD'     : {'Atom':[1,2,3], 4:'K',5:'B'},   # Urey-Bradley force constant and equilibrium distance (Angstrom)
         'MCHARGE'      : {'Atom':[1,2,3], 4:''},          # Atomic charge
         'DIPOLE'       : {0:'X',1:'Y',2:'Z'},             # Dipole moment in local frame
         'QUADX'        : {0:'X'},                         # Quadrupole moment, X component
         'QUADY'        : {0:'X',1:'Y'},                   # Quadrupole moment, Y component
         'QUADZ'        : {0:'X',1:'Y',2:'Z'},             # Quadrupole moment, Y component
         'POLARIZE'     : {'Atom':[1], 2:'A',3:'T'},       # Atomic dipole polarizability
         'BOND-CUBIC'   : {'Atom':[], 0:''},    # Below are global parameters.
         'BOND-QUARTIC' : {'Atom':[], 0:''},
         'ANGLE-CUBIC'  : {'Atom':[], 0:''},
         'ANGLE-QUARTIC': {'Atom':[], 0:''},
         'ANGLE-PENTIC' : {'Atom':[], 0:''},
         'ANGLE-SEXTIC' : {'Atom':[], 0:''},
         'DIELECTRIC'   : {'Atom':[], 0:''},
         'POLAR-SOR'    : {'Atom':[], 0:''}
                                                # Ignored for now: stretch/bend coupling, out-of-plane bending,
                                                # torsional parameters, pi-torsion, torsion-torsion
         }

class Tinker_Reader(BaseReader):
    """Finite state machine for parsing TINKER force field files.

    This class is instantiated when we begin to read in a file.  The
    feed(line) method updates the state of the machine, informing it
    of the current interaction type.  Using this information we can
    look up the interaction type and parameter type for building the
    parameter ID.
    
    """
    
    def __init__(self,fnm):
        super(Tinker_Reader,self).__init__(fnm)
        ## The parameter dictionary (defined in this file)
        self.pdict  = pdict
        ## The atom numbers in the interaction (stored in the TINKER parser)
        self.atom   = []

    def feed(self, line):
        """ Given a line, determine the interaction type and the atoms involved (the suffix).

        TINKER generally has stuff like this:

        @verbatim
        bond-cubic              -2.55
        bond-quartic            3.793125

        vdw           1               3.4050     0.1100
        vdw           2               2.6550     0.0135      0.910 # PRM 4

        multipole     2    1    2               0.25983
                                               -0.03859    0.00000   -0.05818
                                               -0.03673
                                                0.00000   -0.10739
                                               -0.00203    0.00000    0.14412
        @endverbatim

        The '#PRM 4' has no effect on TINKER but it indicates that we
        are tuning the fourth field on the line (the 0.910 value).

        @todo Put the rescaling factors for TINKER parameters in here.
        Currently we're using the initial value to determine the
        rescaling factor which is not very good.

        Every parameter line is prefaced by the interaction type
        except for 'multipole' which is on multiple lines.  Because
        the lines that come after 'multipole' are predictable, we just
        determine the current line using the previous line.

        Random note: Unit of force is kcal / mole / angstrom squared.
        
        """
        s          = line.split()
        self.ln   += 1
        # No sense in doing anything for an empty line or a comment line.
        if len(s) == 0 or match('^#',line): return None, None
        # From the line, figure out the interaction type.  If this line doesn't correspond
        # to an interaction, then do nothing.
        if s[0].upper() in pdict:
            self.itype = s[0].upper()
        # This is kind of a silly hack that allows us to take care of the 'multipole' keyword,
        # because of the syntax that the TINKER .prm file uses.
        elif s[0].upper() == 'MULTIPOLE':
            self.itype = 'MCHARGE'
        elif self.itype == 'MCHARGE':
            self.itype = 'DIPOLE'
        elif self.itype == 'DIPOLE':
            self.itype = 'QUADX'
        elif self.itype == 'QUADX':
            self.itype = 'QUADY'
        elif self.itype == 'QUADY':
            self.itype = 'QUADZ'
        else:
            self.itype = None

        if self.itype in pdict:
            if 'Atom' in pdict[self.itype]:
                # List the atoms in the interaction.
                self.atom = [s[i] for i in pdict[self.itype]['Atom']]
            # The suffix of the parameter ID is built from the atom    #
            # types/classes involved in the interaction.
            self.suffix = '.'.join(self.atom)

def write_key(fout, options, fin=None, defaults={}, verbose=False, prmfnm=None, chk=[]):
    """
    Create or edit a TINKER .key file.
    @param[in] fout Output file name, can be the same as input file name.
    @param[in] options Dictionary containing .key options. Existing options are replaced, new options are added at the end.
    Passing None causes options to be deleted.  To pass an option without an argument, use ''.
    @param[in] fin Input file name.
    @param[in] defaults Default options to add to the mdp only if they don't already exist.
    @param[in] verbose Print out all modifications to the file.
    @param[in] prmfnm TINKER parameter file name.
    @param[in] chk Crash if the key file does NOT have these options by the end.
    """
    # Make sure that the keys are lowercase, and the values are all strings.
    options = OrderedDict([(key.lower(), str(val) if val != None else None) for key, val in options.items()])
    if 'parameters' in options and prmfnm != None:
        raise RuntimeError("Please pass prmfnm or 'parameters':'filename.prm' in options but not both.")
    elif 'parameters' in options:
        prmfnm = options['parameters']
    
    if prmfnm != None:
        # Account for both cases where the file name may end with .prm
        prms = [prmfnm]
        if prms[0].endswith('.prm'):
            prms.append(prmfnm[:-4])
    else:
        prms = []

    # Options that cause the program to crash if they are overwritten
    clashes = []
    # List of lines in the output file.
    out = []
    # List of options in the output file.
    haveopts = []
    skip = 0
    prmflag = 0
    if fin != None and os.path.isfile(fin):
        for line0 in open(fin).readlines():
            line1   = line0.replace('\n','').expandtabs()
            line    = line0.strip().expandtabs()
            s       = line.split()
            if skip:
                out.append(line1)
                skip -= 1
                continue
            # Skip over these three cases:
            # 1) Empty lines get appended to the output and skipped.
            if len(line) == 0 or set(line).issubset([' ']):
                out.append('')
                continue
            # 2) Lines that start with comments are skipped as well.
            if line.startswith("#"):
                out.append(line1)
                continue
            # 3) Lines that correspond to force field parameters are skipped
            if s[0].lower() in allp:
                out.append(line1)
                # 3a) For AMOEBA multipole parameters, skip four additional lines
                if s[0].lower() == "multipole":
                    skip = 4
                continue
            # Now split by the comment character.
            s = line.split('#',1)
            data = s[0]
            comms = s[1] if len(s) > 1 else None
            # Now split off the key and value fields at the space.
            ds = data.split(' ',1)
            keyf = ds[0]
            valf = ds[1] if len(ds) > 1 else ''
            key = keyf.strip().lower()
            haveopts.append(key)
            if key == 'parameters':
                val0 = valf.strip()
                if val0 == '':
                    warn_press_key("Expected a parameter file name but got none")
                # This is the case where "parameters" correctly corresponds to optimize.in
                prmflag = 1
                if prmfnm == None or val0 in prms:
                    out.append(line1)
                    continue
                else:
                    logger.info(line + '\n')
                    warn_press_key("The above line was found in %s, but we expected something like 'parameters %s'; replacing." % (line,prmfnm))
                    options['parameters'] = prmfnm
            if key in options:
                # This line replaces the line in the .key file with the value provided in the dictionary.
                val = options[key]
                val0 = valf.strip()
                if key in clashes and val != val0:
                    raise RuntimeError("write_key tried to set %s = %s but its original value was %s = %s" % (key, val, key, val0))
                # Passing None as the value causes the option to be deleted
                if val == None: 
                    continue
                if len(val) < len(valf):
                    valf = ' ' + val + ' '*(len(valf) - len(val)-1)
                else:
                    valf = ' ' + val + ' '
                lout = [keyf, ' ', valf]
                if comms != None:
                    lout += ['#',comms]
                out.append(''.join(lout))
            else:
                out.append(line1)
    # Options that don't already exist are written at the bottom.
    for key, val in options.items():
        key = key.lower()
        if val == None: continue
        if key not in haveopts:
            haveopts.append(key)
            out.append("%-20s %s" % (key, val))
    # Fill in default options.
    for key, val in defaults.items():
        key = key.lower()
        options[key] = val
        if key not in haveopts:
            haveopts.append(key)
            out.append("%-20s %s" % (key, val))
    # If parameters are not specified, they are printed at the top.
    if not prmflag and prmfnm != None:
        out.insert(0,"parameters %s" % prmfnm)
        options["parameters"] = prmfnm
    elif not prmflag:
        if not os.path.exists('%s.prm' % os.path.splitext(fout)[0]):
            raise RuntimeError('No parameter file detected, this will cause TINKER to crash')
    for i in chk:
        if i not in haveopts:
            raise RuntimeError('%s is expected to be in the .key file, but not found' % i)
    # Finally write the key file.
    file_out = wopen(fout) 
    for line in out:
        print >> file_out, line
    if verbose:
        printcool_dictionary(options, title="%s -> %s with options:" % (fin, fout))
    file_out.close()

class TINKER(Engine):

    """ Engine for carrying out general purpose TINKER calculations. """

    def __init__(self, name="tinker", **kwargs):
        ## Keyword args that aren't in this list are filtered out.
        self.valkwd = ['tinker_key', 'tinkerpath', 'tinker_prm']
        super(TINKER,self).__init__(name=name, **kwargs)

    def setopts(self, **kwargs):
        
        """ Called by __init__ ; Set TINKER-specific options. """

        ## The directory containing TINKER executables (e.g. dynamic)
        if 'tinkerpath' in kwargs:
            self.tinkerpath = kwargs['tinkerpath']
            if not os.path.exists(os.path.join(self.tinkerpath,"dynamic")):
                warn_press_key("The 'dynamic' executable indicated by %s doesn't exist! (Check tinkerpath)" \
                                   % os.path.join(self.tinkerpath,"dynamic"))
        else:
            warn_once("The 'tinkerpath' option was not specified; using default.")
            if which('mdrun') == '':
                warn_press_key("Please add TINKER executables to the PATH or specify tinkerpath.")
            self.tinkerpath = which('dynamic')

    def readsrc(self, **kwargs):

        """ Called by __init__ ; read files from the source directory. """

        self.key = onefile('key', kwargs['tinker_key'] if 'tinker_key' in kwargs else None)
        self.prm = onefile('prm', kwargs['tinker_prm'] if 'tinker_prm' in kwargs else None)
        if 'mol' in kwargs:
            self.mol = kwargs['mol']
        elif 'coords' in kwargs:
            if kwargs['coords'].endswith('.xyz'):
                self.mol = Molecule(kwargs['coords'], ftype="tinker")
            else:
                self.mol = Molecule(kwargs['coords'])
        else:
            arcfile = onefile('arc')
            if not arcfile: raise RuntimeError('Cannot determine which .arc file to use')
            self.mol = Molecule(arcfile)

    def calltinker(self, command, stdin=None, print_to_screen=False, print_command=False, **kwargs):

        """ Call TINKER; prepend the tinkerpath to calling the TINKER program. """

        csplit = command.split()
        # Sometimes the engine changes dirs and the key goes missing, so we link it.
        if "%s.key" % self.name in csplit and not os.path.exists("%s.key" % self.name):
            LinkFile(self.abskey, "%s.key" % self.name)
        prog = os.path.join(self.tinkerpath, csplit[0])
        csplit[0] = prog
        o = _exec(' '.join(csplit), stdin=stdin, print_to_screen=print_to_screen, print_command=print_command, **kwargs)
        for line in o[-10:]:
            # Catch exceptions since TINKER does not have exit status.
            if "TINKER is Unable to Continue" in line:
                for l in o:
                    logger.info("%s\n" % l)
                warn_press_key("TINKER may have crashed! (See above output)")
                break
        return o

    def prepare(self, pbc=False, **kwargs):

        """ Called by __init__ ; prepare the temp directory and figure out the topology. """

        self.rigid = False

        ## Attempt to set some TINKER options.
        tk_chk = []
        tk_opts = OrderedDict([("digits", "10"), ("archive", "")])
        tk_defs = OrderedDict()

        if hasattr(self,'FF'):
            self.FF.make(np.zeros(self.FF.np))
            if self.FF.rigid_water:
                tk_opts["rattle"] = "water"
                self.rigid = True
            if self.FF.amoeba_pol == None:
                raise RuntimeError('You must specify amoeba_pol if there are any AMOEBA forces.')
            elif self.FF.amoeba_pol == 'mutual':
                tk_opts['polarization'] = 'mutual'
                if self.FF.amoeba_eps != None:
                    tk_opts['polar-eps'] = str(self.FF.amoeba_eps)
                else:
                    tk_defs['polar-eps'] = '1e-6'
            elif self.FF.amoeba_pol == 'direct':
                tk_opts['polarization'] = 'direct'
            self.prm = self.FF.tinkerprm
            prmfnm = self.FF.tinkerprm
        elif self.prm:
            prmfnm = self.prm
        else:
            prmfnm = None

        # Periodic boundary conditions may come from the TINKER .key file.
        keypbc = False
        minbox = 1e10
        if self.key:
            for line in open(os.path.join(self.srcdir, self.key)).readlines():
                s = line.split()
                if len(s) > 0 and s[0].lower() == 'a-axis':
                    keypbc = True
                    minbox = float(s[1])
                if len(s) > 0 and s[0].lower() == 'b-axis' and float(s[1]) < minbox:
                    minbox = float(s[1])
                if len(s) > 0 and s[0].lower() == 'c-axis' and float(s[1]) < minbox:
                    minbox = float(s[1])
            if keypbc and (not pbc):
                warn_once("Deleting PBC options from the .key file.")
                tk_opts['a-axis'] = None
                tk_opts['b-axis'] = None
                tk_opts['c-axis'] = None
                tk_opts['alpha'] = None
                tk_opts['beta'] = None
                tk_opts['gamma'] = None
        if (not keypbc) and pbc:
            raise RuntimeError("Periodic boundary conditions require a-axis to be in the .key file.")
        self.pbc = pbc
        if pbc:
            tk_opts['ewald'] = ''
            if minbox <= 10:
                warn_press_key("Periodic box is set to less than 10 Angstroms across")
            # TINKER likes to use up to 7.0 Angstrom for PME cutoffs
            rpme = 0.05*(float(int(minbox - 1))) if minbox <= 15 else 7.0
            tk_defs['ewald-cutoff'] = "%f" % rpme
            # TINKER likes to use up to 9.0 Angstrom for vdW cutoffs
            rvdw = 0.05*(float(int(minbox - 1))) if minbox <= 19 else 9.0
            tk_defs['vdw-cutoff'] = "%f" % rvdw
        else:
            tk_opts['ewald'] = None
            tk_opts['ewald-cutoff'] = None
            tk_opts['vdw-cutoff'] = None
            # This seems to have no effect on the kinetic energy.
            # tk_opts['remove-inertia'] = '0'

        write_key("%s.key" % self.name, tk_opts, os.path.join(self.srcdir, self.key) if self.key else None, tk_defs, verbose=False, prmfnm=prmfnm)
        self.abskey = os.path.abspath("%s.key")

        self.mol[0].write(os.path.join("%s.xyz" % self.name), ftype="tinker")

        ## If the coordinates do not come with TINKER suffixes then throw an error.
        self.mol.require('tinkersuf')

        ## Call analyze to read information needed to build the atom lists.
        o = self.calltinker("analyze %s.xyz P,C" % (self.name))

        ## Parse the output of analyze.
        mode = 0
        self.AtomMask = []
        self.AtomLists = defaultdict(list)
        ptype_dict = {'atom': 'A', 'vsite': 'D'}
        G = nx.Graph()
        for line in o:
            s = line.split()
            if len(s) == 0: continue
            if "Atom Type Definition Parameters" in line:
                mode = 1
            if mode == 1:
                if isint(s[0]): mode = 2
            if mode == 2:
                if isint(s[0]):
                    mass = float(s[5])
                    self.AtomLists['Mass'].append(mass)
                    if mass < 1.0:
                        # Particles with mass less than one count as virtual sites.
                        self.AtomLists['ParticleType'].append('D')
                    else:
                        self.AtomLists['ParticleType'].append('A')
                    self.AtomMask.append(mass >= 1.0)
                else:
                    mode = 0
            if "List of 1-2 Connected Atomic Interactions" in line:
                mode = 3
            if mode == 3:
                if isint(s[0]): mode = 4
            if mode == 4:
                if isint(s[0]):
                    a = int(s[0])
                    b = int(s[1])
                    G.add_node(a)
                    G.add_node(b)
                    G.add_edge(a, b)
                else: mode = 0
        # Use networkx to figure out a list of molecule numbers.
        if len(G.nodes()) > 0:
            # The following code only works in TINKER 6.2
            gs = nx.connected_component_subgraphs(G)
            tmols = [gs[i] for i in np.argsort(np.array([min(g.nodes()) for g in gs]))]
            self.AtomLists['MoleculeNumber'] = [[i+1 in m.nodes() for m in tmols].index(1) for i in range(self.mol.na)]
        else:
            grouped = [i.L() for i in self.mol.molecules]
            self.AtomLists['MoleculeNumber'] = [[i in g for g in grouped].index(1) for i in range(self.mol.na)]
        if hasattr(self,'FF'):
            for f in self.FF.fnms: 
                os.unlink(f)

    def optimize(self, shot=0, method="newton", crit=1e-4):

        """ Optimize the geometry and align the optimized geometry to the starting geometry. """

        if os.path.exists('%s.xyz_2' % self.name):
            os.unlink('%s.xyz_2' % self.name)

        self.mol[shot].write('%s.xyz' % self.name, ftype="tinker")

        if method == "newton":
            if self.rigid: optprog = "optrigid"
            else: optprog = "optimize"
        elif method == "bfgs":
            if self.rigid: optprog = "minrigid"
            else: optprog = "minimize"

        o = self.calltinker("%s %s.xyz %f" % (optprog, self.name, crit))
        # Silently align the optimized geometry.
        M12 = Molecule("%s.xyz" % self.name, ftype="tinker") + Molecule("%s.xyz_2" % self.name, ftype="tinker")
        M12.align(center=False)
        M12[1].write("%s.xyz_2" % self.name, ftype="tinker")
        rmsd = M12.ref_rmsd(0)[1]
        cnvgd = 0
        mode = 0
        for line in o:
            s = line.split()
            if len(s) == 0: continue
            if "Optimally Conditioned Variable Metric Optimization" in line: mode = 1
            if "Limited Memory BFGS Quasi-Newton Optimization" in line: mode = 1
            if mode == 1 and isint(s[0]): mode = 2
            if mode == 2:
                if isint(s[0]): E = float(s[1])
                else: mode = 0
            if "Normal Termination" in line:
                cnvgd = 1
        if not cnvgd:
            for line in o:
                logger.info(str(line) + '\n')
            logger.info("The minimization did not converge in the geometry optimization - printout is above.\n")
        return E, rmsd

    def evaluate_(self, xyzin, force=False, dipole=False):

        """ 
        Utility function for computing energy, and (optionally) forces and dipoles using TINKER. 
        
        Inputs:
        xyzin: TINKER .xyz file name.
        force: Switch for calculating the force.
        dipole: Switch for calculating the dipole.

        Outputs:
        Result: Dictionary containing energies, forces and/or dipoles.
        """

        Result = OrderedDict()
        # If we want the dipoles (or just energies), analyze is the way to go.
        if dipole or (not force):
            oanl = self.calltinker("analyze %s -k %s" % (xyzin, self.name), stdin="G,E", print_to_screen=False)
            # Read potential energy and dipole from file.
            eanl = []
            dip = []
            for line in oanl:
                s = line.split()
                if 'Total Potential Energy : ' in line:
                    eanl.append(float(s[4]) * 4.184)
                if dipole:
                    if 'Dipole X,Y,Z-Components :' in line:
                        dip.append([float(s[i]) for i in range(-3,0)])
            Result["Energy"] = np.array(eanl)
            Result["Dipole"] = np.array(dip)
        # If we want forces, then we need to call testgrad.
        if force:
            E = []
            F = []
            M = Molecule(xyzin)
            boxfile = os.path.split(xyzin)[0]+".box"
            B = [[float(i) for i in line.split()[1:]] for line in open(boxfile).readlines()] if os.path.exists(boxfile) else None
            if B != None and len(B) != len(M):
                raise RuntimeError("Length of box file doesn't match trajectory file")
            if len(M) > 10: warn_once("Using testgrad to loop over %i energy/force calculations; will be slow." % len(self.mol))
            for I in range(len(M)):
                F.append([])
                M[I].write("%s-1.xyz" % self.name, ftype="tinker")
                if B != None:
                    write_key("%s-1.key" % self.name, OrderedDict([('a-axis', B[0]), ('b-axis', B[1]), ('c-axis', B[2]),
                                                                   ('alpha', B[3]), ('beta', B[4]), ('gamma', B[5])]), fin="%s.key" % self.name)
                o = self.calltinker("testgrad %s-1.xyz -k %s%s y n n" % (self.name, self.name, "-1" if B != None else ""))
                # Read data from stdout and stderr, and convert it to GROMACS
                # units for consistency with existing code.
                i = 0
                for line in o:
                    s = line.split()
                    if "Total Potential Energy" in line:
                        E.append(float(s[4]) * 4.184)
                    if len(s) == 6 and all([s[0] == 'Anlyt',isint(s[1]),isfloat(s[2]),isfloat(s[3]),isfloat(s[4]),isfloat(s[5])]):
                        if self.AtomMask[i]:
                            F[-1] += [-1 * float(j) * 4.184 * 10 for j in s[2:5]]
                        i += 1
            Result["Energy"] = np.array(E)
            Result["Force"] = np.array(F)
        return Result

    def energy_force_one(self, shot):

        """ Computes the energy and force using TINKER for one snapshot. """

        self.mol[shot].write("%s.xyz" % self.name, ftype="tinker")
        Result = self.evaluate_("%s.xyz" % self.name, force=True)
        return np.hstack((Result["Energy"].reshape(-1,1), Result["Force"]))

    def energy(self):

        """ Computes the energy using TINKER over a trajectory. """

        if hasattr(self, 'mdtraj') : 
            x = self.mdtraj
        else:
            x = "%s.xyz" % self.name
            self.mol.write(x, ftype="tinker")
        return self.evaluate_(x)["Energy"]

    def energy_force(self):

        """ Computes the energy and force using TINKER over a trajectory. """

        if hasattr(self, 'mdtraj') : 
            x = self.mdtraj
        else:
            x = "%s.xyz" % self.name
            self.mol.write(x, ftype="tinker")
        Result = self.evaluate_(x, force=True)
        return np.hstack((Result["Energy"].reshape(-1,1), Result["Force"]))

    def energy_dipole(self):

        """ Computes the energy and dipole using TINKER over a trajectory. """

        if hasattr(self, 'mdtraj') : 
            x = self.mdtraj
        else:
            x = "%s.xyz" % self.name
            self.mol.write(x, ftype="tinker")
        Result = self.evaluate_(x, dipole=True)
        return np.hstack((Result["Energy"].reshape(-1,1), Result["Dipole"]))

    def normal_modes(self, shot=0, optimize=True):
        # This line actually runs TINKER
        if optimize:
            self.optimize(shot, crit=1e-6)
            o = self.calltinker("vibrate %s.xyz_2 a" % (self.name))
        else:
            warn_once("Asking for normal modes without geometry optimization?")
            self.mol[shot].write('%s.xyz' % self.name, ftype="tinker")
            o = self.calltinker("vibrate %s.xyz a" % (self.name))
        # Read the TINKER output.  The vibrational frequencies are ordered.
        # The six modes with frequencies closest to zero are ignored
        readev = False
        calc_eigvals = []
        calc_eigvecs = []
        for line in o:
            s = line.split()
            if "Vibrational Normal Mode" in line:
                freq = float(s[-2])
                readev = False
                calc_eigvals.append(freq)
                calc_eigvecs.append([])
            elif "Atom" in line and "Delta X" in line:
                readev = True
            elif readev and len(s) == 4 and all([isint(s[0]), isfloat(s[1]), isfloat(s[2]), isfloat(s[3])]):
                calc_eigvecs[-1].append([float(i) for i in s[1:]])
        calc_eigvals = np.array(calc_eigvals)
        calc_eigvecs = np.array(calc_eigvecs)
        # Sort by frequency absolute value and discard the six that are closest to zero
        calc_eigvecs = calc_eigvecs[np.argsort(np.abs(calc_eigvals))][6:]
        calc_eigvals = calc_eigvals[np.argsort(np.abs(calc_eigvals))][6:]
        # Sort again by frequency
        calc_eigvecs = calc_eigvecs[np.argsort(calc_eigvals)]
        calc_eigvals = calc_eigvals[np.argsort(calc_eigvals)]
        os.system("rm -rf *.xyz_* *.[0-9][0-9][0-9]")
        return calc_eigvals, calc_eigvecs

    def multipole_moments(self, shot=0, optimize=True, polarizability=False):

        """ Return the multipole moments of the 1st snapshot in Debye and Buckingham units. """
        
        # This line actually runs TINKER
        if optimize:
            self.optimize(shot, crit=1e-6)
            o = self.calltinker("analyze %s.xyz_2 M" % (self.name))
        else:
            self.mol[shot].write('%s.xyz' % self.name, ftype="tinker")
            o = self.calltinker("analyze %s.xyz M" % (self.name))
        # Read the TINKER output.
        qn = -1
        ln = 0
        for line in o:
            s = line.split()
            if "Dipole X,Y,Z-Components" in line:
                dipole_dict = OrderedDict(zip(['x','y','z'], [float(i) for i in s[-3:]]))
            elif "Quadrupole Moment Tensor" in line:
                qn = ln
                quadrupole_dict = OrderedDict([('xx',float(s[-3]))])
            elif qn > 0 and ln == qn + 1:
                quadrupole_dict['xy'] = float(s[-3])
                quadrupole_dict['yy'] = float(s[-2])
            elif qn > 0 and ln == qn + 2:
                quadrupole_dict['xz'] = float(s[-3])
                quadrupole_dict['yz'] = float(s[-2])
                quadrupole_dict['zz'] = float(s[-1])
            ln += 1

        calc_moments = OrderedDict([('dipole', dipole_dict), ('quadrupole', quadrupole_dict)])

        if polarizability:
            if optimize:
                o = self.calltinker("polarize %s.xyz_2" % (self.name))
            else:
                o = self.calltinker("polarize %s.xyz" % (self.name))
            # Read the TINKER output.
            pn = -1
            ln = 0
            polarizability_dict = OrderedDict()
            for line in o:
                s = line.split()
                if "Total Polarizability Tensor" in line:
                    pn = ln
                elif pn > 0 and ln == pn + 2:
                    polarizability_dict['xx'] = float(s[-3])
                    polarizability_dict['yx'] = float(s[-2])
                    polarizability_dict['zx'] = float(s[-1])
                elif pn > 0 and ln == pn + 3:
                    polarizability_dict['xy'] = float(s[-3])
                    polarizability_dict['yy'] = float(s[-2])
                    polarizability_dict['zy'] = float(s[-1])
                elif pn > 0 and ln == pn + 4:
                    polarizability_dict['xz'] = float(s[-3])
                    polarizability_dict['yz'] = float(s[-2])
                    polarizability_dict['zz'] = float(s[-1])
                ln += 1
            calc_moments['polarizability'] = polarizability_dict
        os.system("rm -rf *.xyz_* *.[0-9][0-9][0-9]")
        return calc_moments

    def energy_rmsd(self, shot=0, optimize=True):

        """ Calculate energy of the selected structure (optionally minimize and return the minimized energy and RMSD). In kcal/mol. """

        rmsd = 0.0
        # This line actually runs TINKER
        # xyzfnm = sysname+".xyz"
        if optimize:
            E_, rmsd = self.optimize(shot)
            o = self.calltinker("analyze %s.xyz_2 E" % self.name)
            #----
            # Two equivalent ways to get the RMSD, here for reference.
            #----
            # M1 = Molecule("%s.xyz" % self.name, ftype="tinker")
            # M2 = Molecule("%s.xyz_2" % self.name, ftype="tinker")
            # M1 += M2
            # rmsd = M1.ref_rmsd(0)[1]
            #----
            # oo = self.calltinker("superpose %s.xyz %s.xyz_2 1 y u n 0" % (self.name, self.name))
            # for line in oo:
            #     if "Root Mean Square Distance" in line:
            #         rmsd = float(line.split()[-1])
            #----
            os.system("rm %s.xyz_2" % self.name)
        else:
            o = self.calltinker("analyze %s.xyz E" % self.name)
        # Read the TINKER output. 
        E = None
        for line in o:
            if "Total Potential Energy" in line:
                E = float(line.split()[-2].replace('D','e'))
        if E == None:
            raise RuntimeError("Total potential energy wasn't encountered when calling analyze!")
        if optimize and abs(E-E_) > 0.1:
            warn_press_key("Energy from optimize and analyze aren't the same (%.3f vs. %.3f)" % (E, E_))
        return E, rmsd

    def interaction_energy(self, fraga, fragb):
        
        """ Calculate the interaction energy for two fragments. """

        self.A = TINKER(name="A", mol=self.mol.atom_select(fraga), tinker_key="%s.key" % self.name, tinkerpath=self.tinkerpath)
        self.B = TINKER(name="B", mol=self.mol.atom_select(fragb), tinker_key="%s.key" % self.name, tinkerpath=self.tinkerpath)

        # Interaction energy needs to be in kcal/mol.
        return (self.energy() - self.A.energy() - self.B.energy()) / 4.184

    def molecular_dynamics(self, nsteps, timestep, temperature=None, pressure=None, nequil=0, nsave=1000, minimize=True, anisotropic=False, threads=1, verbose=False, **kwargs):
        
        """
        Method for running a molecular dynamics simulation.  

        Required arguments:
        nsteps      = (int)   Number of total time steps
        timestep    = (float) Time step in FEMTOSECONDS
        temperature = (float) Temperature control (Kelvin)
        pressure    = (float) Pressure control (atmospheres)
        nequil      = (int)   Number of additional time steps at the beginning for equilibration
        nsave       = (int)   Step interval for saving and printing data
        minimize    = (bool)  Perform an energy minimization prior to dynamics
        threads     = (int)   Specify how many OpenMP threads to use

        Returns simulation data:
        Rhos        = (array)     Density in kilogram m^-3
        Potentials  = (array)     Potential energies
        Kinetics    = (array)     Kinetic energies
        Volumes     = (array)     Box volumes
        Dips        = (3xN array) Dipole moments
        EComps      = (dict)      Energy components
        """

        md_defs = OrderedDict()
        md_opts = OrderedDict()
        # Print out averages only at the end.
        md_opts["printout"] = nsave
        md_opts["openmp-threads"] = threads
        # Langevin dynamics for temperature control.
        if temperature:
            md_defs["integrator"] = "stochastic"
        else:
            md_defs["integrator"] = "beeman"
            md_opts["thermostat"] = None
        # Periodic boundary conditions.
        if self.pbc:
            md_opts["vdw-correction"] = ''
            md_opts["mpole-list"] = ''
            if temperature and pressure: 
                md_defs["integrator"] = "nose-hoover"
                md_defs["thermostat"] = "nose-hoover"
                md_defs["barostat"] = "nose-hoover"
                # md_defs["barostat"] = "montecarlo"
                # md_defs["volume-trial"] = "10"
                # md_opts["save-box"] = ''
                if anisotropic:
                    md_opts["aniso-pressure"] = ''
            elif pressure:
                warn_once("Pressure is ignored because temperature is turned off.")
        else:
            if pressure:
                warn_once("Pressure is ignored because pbc is set to False.")
            # Use stochastic dynamics for the gas phase molecule.
            # If we use the regular integrators it may miss
            # six degrees of freedom in calculating the kinetic energy.
            md_opts["barostat"] = None

        if minimize:
            if verbose: logger.info("Minimizing the energy...")
            self.optimize(method="bfgs", crit=1)
            os.system("mv %s.xyz_2 %s.xyz" % (self.name, self.name))
            if verbose: logger.info("Done\n")

        # Run equilibration.
        if nequil > 0:
            write_key("%s-eq.key" % self.name, md_opts, "%s.key" % self.name, md_defs)
            if verbose: printcool("Running equilibration dynamics", color=0)
            if self.pbc and pressure:
                self.calltinker("dynamic %s -k %s-eq %i %f %f 4 %f %f" % (self.name, self.name, nequil, timestep, float(nsave*timestep)/1000, 
                                                                          temperature, pressure), print_to_screen=verbose)
            else:
                self.calltinker("dynamic %s -k %s-eq %i %f %f 2 %f" % (self.name, self.name, nequil, timestep, float(nsave*timestep)/1000,
                                                                       temperature), print_to_screen=verbose)
            os.system("rm -f %s.arc %s.box" % (self.name, self.name))

        # Run production.
        if verbose: printcool("Running production dynamics", color=0)
        write_key("%s-md.key" % self.name, md_opts, "%s.key" % self.name, md_defs)
        if self.pbc and pressure:
            odyn = self.calltinker("dynamic %s -k %s-md %i %f %f 4 %f %f" % (self.name, self.name, nsteps, timestep, float(nsave*timestep/1000), 
                                                                             temperature, pressure), print_to_screen=verbose)
        else:
            odyn = self.calltinker("dynamic %s -k %s-md %i %f %f 2 %f" % (self.name, self.name, nsteps, timestep, float(nsave*timestep/1000), 
                                                                          temperature), print_to_screen=verbose)
            
        # Gather information.
        os.system("mv %s.arc %s-md.arc" % (self.name, self.name))
        if self.pbc: os.system("mv %s.box %s-md.box" % (self.name, self.name))
        self.mdtraj = "%s-md.arc" % self.name
        edyn = []
        kdyn = []
        temps = []
        for line in odyn:
            s = line.split()
            if 'Current Potential' in line:
                edyn.append(float(s[2]))
            if 'Current Kinetic' in line:
                kdyn.append(float(s[2]))
            if len(s) > 0 and s[0] == 'Temperature' and s[2] == 'Kelvin':
                temps.append(float(s[1]))

        # Potential and kinetic energies converted to kJ/mol.
        edyn = np.array(edyn) * 4.184
        kdyn = np.array(kdyn) * 4.184
        temps = np.array(temps)
    
        if verbose: logger.info("Post-processing to get the dipole moments\n")
        oanl = self.calltinker("analyze %s-md.arc" % self.name, stdin="G,E", print_to_screen=False)

        # Read potential energy and dipole from file.
        eanl = []
        dip = []
        mass = 0.0
        ecomp = OrderedDict()
        havekeys = set()
        first_shot = True
        for ln, line in enumerate(oanl):
            strip = line.strip()
            s = line.split()
            if 'Total System Mass' in line:
                mass = float(s[-1])
            if 'Total Potential Energy : ' in line:
                eanl.append(float(s[4]))
            if 'Dipole X,Y,Z-Components :' in line:
                dip.append([float(s[i]) for i in range(-3,0)])
            if first_shot:
                for key in eckeys:
                    if strip.startswith(key):
                        if key in ecomp:
                            ecomp[key].append(float(s[-2])*4.184)
                        else:
                            ecomp[key] = [float(s[-2])*4.184]
                        if key in havekeys:
                            first_shot = False
                        havekeys.add(key)
            else:
                for key in havekeys:
                    if strip.startswith(key):
                        if key in ecomp:
                            ecomp[key].append(float(s[-2])*4.184)
                        else:
                            ecomp[key] = [float(s[-2])*4.184]
        for key in ecomp:
            ecomp[key] = np.array(ecomp[key])
        ecomp["Potential Energy"] = edyn
        ecomp["Kinetic Energy"] = kdyn
        ecomp["Temperature"] = temps
        ecomp["Total Energy"] = edyn+kdyn

        # Energies in kilojoules per mole
        eanl = np.array(eanl) * 4.184
        # Dipole moments in debye
        dip = np.array(dip)
        # Volume of simulation boxes in cubic nanometers
        # Conversion factor derived from the following:
        # In [22]: 1.0 * gram / mole / (1.0 * nanometer)**3 / AVOGADRO_CONSTANT_NA / (kilogram/meter**3)
        # Out[22]: 1.6605387831627252
        conv = 1.6605387831627252
        if self.pbc:
            box = [[float(i) for i in line.split()[1:4]] for line in open("%s-md.box" % self.name).readlines()]
            vol = np.array([i[0]*i[1]*i[2] for i in box]) / 1000
            rho = conv * mass / vol
        else:
            vol = None
            rho = None

        return rho, edyn, kdyn, vol, dip, ecomp

class Liquid_TINKER(Liquid):
    """ Condensed phase property matching using TINKER. """
    def __init__(self,options,tgt_opts,forcefield):
        # Number of threads in running "dynamic".
        self.set_option(tgt_opts,'md_threads')
        # Number of threads in running "dynamic".
        self.set_option(options,'tinkerpath')
        # Name of the liquid coordinate file.
        self.set_option(tgt_opts,'liquid_coords',default='liquid.xyz',forceprint=True)
        # Name of the gas coordinate file.
        self.set_option(tgt_opts,'gas_coords',default='gas.xyz',forceprint=True)
        # Class for creating engine object.
        self.engine_ = TINKER
        # Name of the engine to pass to npt.py.
        self.engname = "tinker"
        # Command prefix.
        self.nptpfx = ""
        # Extra files to be linked into the temp-directory.
        self.nptfiles = ['%s.key' % os.path.splitext(f)[0] for f in [self.liquid_coords, self.gas_coords]]
        # Set some options for the polarization correction calculation.
        self.gas_engine_args = {'tinker_key' : '%s.key' % os.path.splitext(self.gas_coords)[0]}
        # Scripts to be copied from the ForceBalance installation directory.
        self.scripts = []
        # Initialize the base class.
        super(Liquid_TINKER,self).__init__(options,tgt_opts,forcefield)
        # Error checking.
        for i in self.nptfiles:
            if not os.path.exists(os.path.join(self.root, self.tgtdir, i)):
                raise RuntimeError('Please provide %s; it is needed to proceed.' % i)
        # Send back the trajectory file.
        if self.save_traj > 0:
            self.extra_output = ['liquid-md.arc']
        # Dictionary of .dyn files used to restart simulations.
        self.DynDict = OrderedDict()
        self.DynDict_New = OrderedDict()

    def npt_simulation(self, temperature, pressure, simnum):
        """ Submit a NPT simulation to the Work Queue. """
        if GoodStep() and (temperature, pressure) in self.DynDict_New:
            self.DynDict[(temperature, pressure)] = self.DynDict_New[(temperature, pressure)]
        if (temperature, pressure) in self.DynDict:
            dynsrc = self.DynDict[(temperature, pressure)]
            dyndest = os.path.join(os.getcwd(), 'liquid.dyn')
            logger.info("Copying .dyn file: %s to %s\n" % (dynsrc, dyndest))
            shutil.copy2(dynsrc,dyndest)
            self.nptfiles.append(dyndest)
        self.DynDict_New[(temperature, pressure)] = os.path.join(os.getcwd(),'liquid.dyn')
        super(Liquid_TINKER, self).npt_simulation(temperature, pressure, simnum)

class AbInitio_TINKER(AbInitio):
    """ Subclass of Target for force and energy matching using TINKER. """
    def __init__(self,options,tgt_opts,forcefield):
        ## Default file names for coordinates and key file.
        self.set_option(tgt_opts,'coords',default="all.arc")
        self.set_option(tgt_opts,'tinker_key',default="shot.key")
        self.engine_ = TINKER
        ## Initialize base class.
        super(AbInitio_TINKER,self).__init__(options,tgt_opts,forcefield)

class BindingEnergy_TINKER(BindingEnergy):
    """ Binding energy matching using TINKER. """
    def __init__(self,options,tgt_opts,forcefield):
        self.engine_ = TINKER
        ## Initialize base class.
        super(BindingEnergy_TINKER,self).__init__(options,tgt_opts,forcefield)

class Interaction_TINKER(Interaction):
    """ Subclass of Target for interaction matching using TINKER. """
    def __init__(self,options,tgt_opts,forcefield):
        ## Default file names for coordinates and key file.
        self.set_option(tgt_opts,'coords',default="all.arc")
        self.set_option(tgt_opts,'tinker_key',default="shot.key")
        self.engine_ = TINKER
        ## Initialize base class.
        super(Interaction_TINKER,self).__init__(options,tgt_opts,forcefield)

class Moments_TINKER(Moments):
    """ Subclass of Target for multipole moment matching using TINKER. """
    def __init__(self,options,tgt_opts,forcefield):
        ## Default file names for coordinates and key file.
        self.set_option(tgt_opts,'coords',default="input.xyz")
        self.set_option(tgt_opts,'tinker_key',default="input.key")
        self.engine_ = TINKER
        ## Initialize base class.
        super(Moments_TINKER,self).__init__(options,tgt_opts,forcefield)

class Vibration_TINKER(Vibration):
    """ Vibrational frequency matching using TINKER. """
    def __init__(self,options,tgt_opts,forcefield):
        ## Default file names for coordinates and key file.
        self.set_option(tgt_opts,'coords',default="input.xyz")
        self.set_option(tgt_opts,'tinker_key',default="input.key")
        self.engine_ = TINKER
        ## Initialize base class.
        super(Vibration_TINKER,self).__init__(options,tgt_opts,forcefield)

