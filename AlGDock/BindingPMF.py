#!/usr/bin/env python

# TODO: Free energy of external confinement for poseBPMFs

import os
import cPickle as pickle
import gzip
import copy

import sys
import time
import numpy as np

from collections import OrderedDict

import MMTK
import MMTK.Units
from MMTK.ParticleProperties import Configuration
from MMTK.ForceFields import ForceField

import Scientific
try:
  from Scientific._vector import Vector
except:
  from Scientific.Geometry.VectorModule import Vector
  
import AlGDock as a
# Define allowed_phases list and arguments dictionary
from AlGDock.BindingPMF_arguments import *
# Define functions: merge_dictionaries, convert_dictionary_relpath, and dict_view
from AlGDock.DictionaryTools import *

import pymbar.timeseries

import multiprocessing
from multiprocessing import Process

try:
  import requests # for downloading additional files
except:
  print '  no requests module for downloading additional files'

# For profiling. Unnecessary for normal execution.
# from memory_profiler import profile

#############
# Constants #
#############

R = 8.3144621 * MMTK.Units.J / MMTK.Units.mol / MMTK.Units.K

term_map = {
  'cosine dihedral angle':'MM',
  'electrostatic/pair sum':'MM',
  'harmonic bond':'MM',
  'harmonic bond angle':'MM',
  'Lennard-Jones':'MM',
  'OpenMM':'MM',
  'OBC':'OBC',
  'OBC_desolv':'OBC',
  'site':'site',
  'sLJr':'sLJr',
  'sELE':'sELE',
  'sLJa':'sLJa',
  'LJr':'LJr',
  'LJa':'LJa',
  'ELE':'ELE',
  'pose dihedral angle':'k_angular_int',
  'pose external dihedral':'k_angular_ext',
  'pose external distance':'k_spatial_ext',
  'pose external angle':'k_angular_ext'}

# In APBS, minimum ratio of PB grid length to maximum dimension of solute
LFILLRATIO = 4.0 # For the ligand
RFILLRATIO = 2.0 # For the receptor/complex

DEBUG = False

########################
# Auxilliary functions #
########################

def HMStime(s):
  """
  Given the time in seconds, an appropriately formatted string.
  """
  if s<60.:
    return '%.2f s'%s
  elif s<3600.:
    return '%d:%.2f'%(int(s/60%60),s%60)
  else:
    return '%d:%d:%.2f'%(int(s/3600),int(s/60%60),s%60)

class NullDevice():
  """
  A device to suppress output
  """
  def write(self, s):
    pass

##############
# Main Class #
##############

class BPMF:
  def __init__(self, **kwargs):
    """Parses the input arguments and runs the requested docking calculation"""
    
    # Set undefined keywords to None
    for key in arguments.keys():
      if not key in kwargs.keys():
        kwargs[key] = None
    if kwargs['dir_grid'] is None:
      kwargs['dir_grid'] = ''

    mod_path = os.path.join(os.path.dirname(a.__file__),'BindingPMF.py')
    print """###########
# AlGDock #
###########
Molecular docking with adaptively scaled alchemical interaction grids

in {0}
last modified {1}
    """.format(mod_path, time.ctime(os.path.getmtime(mod_path)))
    
    # Multiprocessing options.
    # Default is to use 1 core.
    # If cores is a number, then that number (or the maximum number)
    # of cores will be used.
    
    # Default
    available_cores = multiprocessing.cpu_count()
    if kwargs['cores'] is None:
      self._cores = 1
    elif (kwargs['cores']==-1):
      self._cores = available_cores
    else:
      self._cores = min(kwargs['cores'], available_cores)
    print "using %d/%d available cores"%(self._cores, available_cores)

    if kwargs['rotate_matrix'] is not None:
      self._view_args_rotate_matrix = kwargs['rotate_matrix']

    if kwargs['random_seed'] is None:
      self._random_seed = 0
    else:
      self._random_seed = kwargs['random_seed']
      print 'using random number seed of %d'%self._random_seed

    self.confs = {'cool':{}, 'dock':{}}
    
    self.dir = {}
    self.dir['start'] = os.getcwd()
    
    if kwargs['dir_dock'] is not None:
      self.dir['dock'] = os.path.abspath(kwargs['dir_dock'])
    else:
      self.dir['dock'] = os.path.abspath('.')
    
    if kwargs['dir_cool'] is not None:
      self.dir['cool'] = os.path.abspath(kwargs['dir_cool'])
    else:
      self.dir['cool'] = self.dir['dock'] # Default that may be
                                          # overwritten by stored directory
    
    # Load previously stored file names and arguments
    FNs = OrderedDict()
    args = OrderedDict()
    for p in ['dock','cool']:
      params = self._load(p, kwargs['pose'])
      if params is not None:
        (fn_dict, arg_dict) = params
        FNs[p] = convert_dictionary_relpath(fn_dict,
          relpath_o=self.dir[p], relpath_n=None)
        args[p] = arg_dict
        if (p=='dock') and (kwargs['dir_cool'] is None) and \
           ('dir_cool' in FNs[p].keys()) and \
           (FNs[p]['dir_cool'] is not None):
          self.dir['cool'] = FNs[p]['dir_cool']
      else:
        FNs[p] = OrderedDict()
        args[p] = OrderedDict()
  
    print '\n*** Directories ***'
    print dict_view(self.dir)
  
    # Identify tarballs
    tarFNs = [kwargs[prefix + '_tarball'] \
      for prefix in ['ligand','receptor','complex'] \
      if (prefix + '_tarball') in kwargs.keys() and
      kwargs[(prefix + '_tarball')] is not None]
    for p in ['cool','dock']:
      if (p in FNs.keys()) and ('tarball' in FNs[p].keys()):
        tarFNs += [tarFN for tarFN in FNs[p]['tarball'].values() \
          if tarFN is not None]
    tarFNs = set([FN for FN in tarFNs if os.path.isfile(FN)])

    # Identify files to look for in the tarballs
    seekFNs = []
    if len(tarFNs)>0:
      # From the keyword arguments
      for prefix in ['ligand','receptor','complex']:
        for postfix in ('database','prmtop','inpcrd','mol2','fixed_atoms'):
          key = '%s_%s'%(prefix,postfix)
          if (key in kwargs.keys()) and (kwargs[key] is not None):
            FN = os.path.abspath(kwargs[key])
            if not os.path.isfile(FN):
              seekFNs.append(os.path.basename(FN))
      # From files in a previous instance
      for p in ['cool','dock']:
        if p in FNs.keys():
          for level1 in ['ligand_database','prmtop','inpcrd','fixed_atoms']:
            if level1 in FNs[p].keys():
              if isinstance(FNs[p][level1],dict):
                for level2 in ['L','R','RL']:
                  if level2 in FNs[p][level1].keys():
                    seekFNs.append(os.path.basename(FNs[p][level1][level2]))
              else:
                seekFNs.append(os.path.basename(FNs[p][level1]))
      seekFNs = set(seekFNs)
    seek_frcmod = (kwargs['frcmodList'] is None) or \
      (not os.path.isfile(kwargs['frcmodList'][0]))

    # Decompress tarballs into self.dir['dock']
    self._toClear = []

    if len(seekFNs)>0:
      import tarfile

      print ">>> Decompressing tarballs"
      print 'looking for:\n  ' + '\n  '.join(seekFNs)
      if seek_frcmod:
        print '  and frcmod files'

      for tarFN in tarFNs:
        print 'reading '+tarFN
        tarF = tarfile.open(tarFN,'r')
        for member in tarF.getmembers():
          for seekFN in seekFNs:
            if member.name.endswith(seekFN):
              tarF.extract(member, path = self.dir['dock'])
              self._toClear.append(os.path.join(self.dir['dock'],seekFN))
              print '  extracted '+seekFN
          if seek_frcmod and member.name.endswith('frcmod'):
            FN = os.path.abspath(os.path.join(self.dir['dock'],member.name))
            if not os.path.isfile(FN):
              tarF.extract(member, path = self.dir['dock'])
              kwargs['frcmodList'] = [FN]
              self._toClear.append(FN)
              print '  extracted '+FN
      print

    # Set up file name dictionary
    print '*** Files ***'

    for p in ['cool','dock']:
      if p in FNs.keys():
        if FNs[p]!={}:
          print 'previously stored in %s directory:'%p
          print dict_view(FNs[p], relpath=self.dir['start'])

    if not (FNs['cool']=={} and FNs['dock']=={}):
      print 'from arguments and defaults:'

    def cdir_or_dir_dock(FN):
      if FN is not None:
        return a.findPath([FN,os.path.join(self.dir['dock'],FN)])
      else:
        return None

    if kwargs['frcmodList'] is not None:
      if isinstance(kwargs['frcmodList'],str):
        kwargs['frcmodList'] = [kwargs['frcmodList']]
      kwargs['frcmodList'] = [cdir_or_dir_dock(FN) \
        for FN in kwargs['frcmodList']]
  
    FFpath = a.search_paths['gaff'] \
      if 'gaff' in a.search_paths.keys() else []
    FNs['new'] = OrderedDict([
      ('ligand_database',cdir_or_dir_dock(kwargs['ligand_database'])),
      ('forcefield',a.findPath(\
        [kwargs['forcefield'],'../Data/gaff2.dat'] + FFpath)),
      ('frcmodList',kwargs['frcmodList']),
      ('tarball',OrderedDict([
        ('L',a.findPath([kwargs['ligand_tarball']])),
        ('R',a.findPath([kwargs['receptor_tarball']])),
        ('RL',a.findPath([kwargs['complex_tarball']]))])),
      ('prmtop',OrderedDict([
        ('L',cdir_or_dir_dock(kwargs['ligand_prmtop'])),
        ('R',cdir_or_dir_dock(kwargs['receptor_prmtop'])),
        ('RL',cdir_or_dir_dock(kwargs['complex_prmtop']))])),
      ('inpcrd',OrderedDict([
        ('L',cdir_or_dir_dock(kwargs['ligand_inpcrd'])),
        ('R',cdir_or_dir_dock(kwargs['receptor_inpcrd'])),
        ('RL',cdir_or_dir_dock(kwargs['complex_inpcrd']))])),
      ('mol2',OrderedDict([
        ('L',cdir_or_dir_dock(kwargs['ligand_mol2']))])),
      ('fixed_atoms',OrderedDict([
        ('R',cdir_or_dir_dock(kwargs['receptor_fixed_atoms'])),
        ('RL',cdir_or_dir_dock(kwargs['complex_fixed_atoms']))])),
      ('grids',OrderedDict([
        ('LJr',a.findPath([kwargs['grid_LJr'],
          os.path.join(kwargs['dir_grid'],'LJr.nc'),
          os.path.join(kwargs['dir_grid'],'LJr.dx'),
          os.path.join(kwargs['dir_grid'],'LJr.dx.gz')])),
        ('LJa',a.findPath([kwargs['grid_LJa'],
          os.path.join(kwargs['dir_grid'],'LJa.nc'),
          os.path.join(kwargs['dir_grid'],'LJa.dx'),
          os.path.join(kwargs['dir_grid'],'LJa.dx.gz')])),
        ('ELE',a.findPath([kwargs['grid_ELE'],
          os.path.join(kwargs['dir_grid'],'electrostatic.nc'),
          os.path.join(kwargs['dir_grid'],'electrostatic.dx'),
          os.path.join(kwargs['dir_grid'],'electrostatic.dx.gz'),
          os.path.join(kwargs['dir_grid'],'pb.nc'),
          os.path.join(kwargs['dir_grid'],'pb.dx'),
          os.path.join(kwargs['dir_grid'],'pb.dx.gz'),
          os.path.join(kwargs['dir_grid'],'pbsa.nc')])),
        ('desolv',a.findPath([kwargs['grid_desolv'],
          os.path.join(kwargs['dir_grid'],'desolv.nc'),
          os.path.join(kwargs['dir_grid'],'desolv.dx'),
          os.path.join(kwargs['dir_grid'],'desolv.dx.gz')]))])),
      ('score','default' if kwargs['score']=='default' \
                            else a.findPath([kwargs['score']])),
      ('dir_cool',self.dir['cool'])])

    if not (FNs['cool']=={} and FNs['dock']=={}):
      print dict_view(FNs['new'], relpath=self.dir['start'])
      print 'to be used:'

    self._FNs = merge_dictionaries(
      [FNs[src] for src in ['new','cool','dock']])
  
    # Default: a force field modification is in the same directory as the ligand
    if (self._FNs['frcmodList'] is None):
      if self._FNs['prmtop']['L'] is not None:
        dir_lig = os.path.dirname(self._FNs['prmtop']['L'])
        frcmodpaths = [os.path.abspath(os.path.join(dir_lig, \
          os.path.basename(self._FNs['prmtop']['L'])[:-7]+'.frcmod'))]
      else:
        dir_lig = '.'
        frcmodpaths = []
      if kwargs['frcmodList'] is None:
        frcmodpaths.extend([\
          os.path.abspath(os.path.join(dir_lig,'lig.frcmod')),\
          os.path.abspath(os.path.join(dir_lig,'ligand.frcmod'))])
        frcmod = a.findPath(frcmodpaths)
        self._FNs['frcmodList'] = [frcmod]
    elif not isinstance(self._FNs['frcmodList'],list):
      self._FNs['frcmodList'] = [self._FNs['frcmodList']]

    # Check for existence of required files
    do_dock = (hasattr(args,'run_type') and \
              (args.run_type not in ['store_params', 'cool']))

    for key in ['ligand_database','forcefield']:
      if (self._FNs[key] is None) or (not os.path.isfile(self._FNs[key])):
        raise Exception('File for %s is missing!'%key)

    for (key1,key2) in [('prmtop','L'),('inpcrd','L')]:
      FN = self._FNs[key1][key2]
      if (FN is None) or (not os.path.isfile(FN)):
        raise Exception('File for %s %s is missing'%(key1,key2))

    for (key1,key2) in [\
        ('prmtop','RL'), ('inpcrd','RL'), \
        ('grids','LJr'), ('grids','LJa'), ('grids','ELE')]:
      FN = self._FNs[key1][key2]
      errstring = 'Missing file %s %s required for docking!'%(key1,key2)
      if (FN is None) or (not os.path.isfile(FN)):
        if do_dock:
          raise Exception(errstring)
        else:
          print errstring

    if ((self._FNs['inpcrd']['RL'] is None) and \
        (self._FNs['inpcrd']['R'] is None)):
        if do_dock:
          raise Exception('Receptor coordinates needed for docking!')
        else:
          print 'Receptor coordinates needed for docking!'

    print dict_view(self._FNs, relpath=self.dir['start'], show_None=True)
    
    args['default_cool'] = OrderedDict([
        ('protocol','Adaptive'),
        ('therm_speed',30.0),
        ('T_HIGH',600.),
        ('T_SIMMIN',300.),
        ('T_TARGET',300.),
        ('H_mass',4.0),
        ('fraction_CD',0.5),
        ('CD_steps_per_trial',5),
        ('delta_t_CD',4.0),
        ('delta_t',4.0),
        ('sampler','NUTS'),
        ('steps_per_seed',1000),
        ('seeds_per_state',50),
        ('darts_per_seed',0),
        ('repX_cycles',20),
        ('min_repX_acc',0.4),
        ('sweeps_per_cycle',1000),
        ('snaps_per_cycle',50),
        ('attempts_per_sweep',25),
        ('steps_per_sweep',50),
        ('darts_per_sweep',0),
        ('phases',['NAMD_Gas','NAMD_OBC']),
        ('sampling_importance_resampling',False),
        ('solvation','Desolvated'),
        ('keep_intermediate',False),
        ('GMC_attempts', 0),
        ('GMC_tors_threshold', 0.0)])

    args['default_dock'] = OrderedDict(args['default_cool'].items() + [
      ('temperature_scaling','Linear'),
      ('site',None),
      ('site_center',None),
      ('site_direction',None),
      ('site_max_Z',None),
      ('site_max_R',None),
      ('site_density',50.),
      ('site_measured',None),
      ('pose',-1),
      ('k_pose', 1000.0 * MMTK.Units.kJ / MMTK.Units.mol / MMTK.Units.K),
      ('MCMC_moves',1),
      ('rmsd',False)] + \
      [('receptor_'+phase,None) for phase in allowed_phases])
    args['default_dock']['snaps_per_cycle'] = 50

    # Store passed arguments in dictionary
    for p in ['cool','dock']:
      args['new_'+p] = OrderedDict()
      for key in args['default_'+p].keys():
        specific_key = p + '_' + key
        if (specific_key in kwargs.keys()) and \
           (kwargs[specific_key] is not None):
          # Use the specific key if it exists
          args['new_'+p][key] = kwargs[specific_key]
        elif (key in ['site_center', 'site_direction'] +
                     ['receptor_'+phase for phase in allowed_phases]) and \
             (kwargs[key] is not None):
          # Convert these to arrays of floats
          args['new_'+p][key] = np.array(kwargs[key], dtype=float)
        elif key in kwargs.keys():
          # Use the general key
          args['new_'+p][key] = kwargs[key]

    self.params = OrderedDict()
    for p in ['cool','dock']:
      self.params[p] = merge_dictionaries(
        [args[src] for src in ['new_'+p,p,'default_'+p]])

    # Check that phases are permitted
    for phase in (self.params['cool']['phases'] + self.params['dock']['phases']):
      if phase not in allowed_phases:
        raise Exception(phase + ' phase is not supported!')
        
    # Make sure prerequistite phases are included:
    #   sander_Gas is necessary for any sander or gbnsr6 phase
    #   NAMD_Gas is necessary for APBS_PBSA
    for process in ['cool','dock']:
      phase_list = self.params[process]['phases']
      if (not 'sander_Gas' in phase_list) and \
          len([p for p in phase_list \
            if p.startswith('sander') or p.startswith('gbnsr6')])>0:
        phase_list.append('sander_Gas')
      if (not 'NAMD_Gas' in phase_list) and ('APBS_PBSA' in phase_list):
        phase_list.append('NAMD_Gas')
  
    self._scalables = ['OBC','sLJr','sELE','LJr','LJa','ELE']

    # Variables dependent on the parameters
    self.original_Es = [[{}]]
    for phase in allowed_phases:
      if self.params['dock']['receptor_'+phase] is not None:
        self.original_Es[0][0]['R'+phase] = \
          np.atleast_2d(self.params['dock']['receptor_'+phase])
      else:
        self.original_Es[0][0]['R'+phase] = None
        
    self.T_HIGH = self.params['cool']['T_HIGH']
    self.T_SIMMIN = self.params['cool']['T_SIMMIN']
    self.RT_SIMMIN = R * self.params['cool']['T_SIMMIN']
    self.T_TARGET = self.params['cool']['T_TARGET']
    self.RT_TARGET = R * self.params['cool']['T_TARGET']

    self._setup_universe(do_dock = do_dock)

    print '\n*** Simulation parameters and constants ***'
    for p in ['cool','dock']:
      print '\nfor %s:'%p
      print dict_view(self.params[p])[:-1]

    self.timings = {'max':kwargs['max_time']}
    self.start_times = {}
    self._run(kwargs['run_type'])
      
  def _setup_universe(self, do_dock=True):
    """Creates an MMTK InfiniteUniverse and adds the ligand"""
  
    # Set up the system
    original_stderr = sys.stderr
    sys.stderr = NullDevice()
    MMTK.Database.molecule_types.directory = \
      os.path.dirname(self._FNs['ligand_database'])
    self.molecule = MMTK.Molecule(\
      os.path.basename(self._FNs['ligand_database']))
    sys.stderr = original_stderr
    
    # Hydrogen Mass Repartitioning
    # (sets hydrogen mass to H_mass and scales other masses down)
    if self.params['cool']['H_mass']>0.:
      from AlGDock.HMR import hydrogen_mass_repartitioning
      self.molecule = hydrogen_mass_repartitioning(self.molecule, \
        self.params['cool']['H_mass'])

    # Helpful variables for referencing and indexing atoms in the molecule
    self.molecule.heavy_atoms = [ind for (atm,ind) in \
      zip(self.molecule.atoms,range(self.molecule.numberOfAtoms())) \
      if atm.type.name!='hydrogen']
    self.molecule.nhatoms = len(self.molecule.heavy_atoms)

    self.molecule.prmtop_atom_order = np.array([atom.number \
      for atom in self.molecule.prmtop_order], dtype=int)
    self.molecule.inv_prmtop_atom_order = \
      np.zeros(shape=len(self.molecule.prmtop_atom_order), dtype=int)
    for i in range(len(self.molecule.prmtop_atom_order)):
      self.molecule.inv_prmtop_atom_order[self.molecule.prmtop_atom_order[i]] = i

    # Create universe and add molecule to universe
    self.universe = MMTK.Universe.InfiniteUniverse()
    self.universe.addObject(self.molecule)
    self._evaluators = {} # Store evaluators
    self._OpenMM_sims = {} # Store OpenMM simulations
    self._ligand_natoms = self.universe.numberOfAtoms()

    # Force fields
    self._forceFields = {}
    
    # Molecular mechanics force fields
    from MMTK.ForceFields import Amber12SBForceField
    self._forceFields['gaff'] = Amber12SBForceField(
      parameter_file=self._FNs['forcefield'],mod_files=self._FNs['frcmodList'])

    # Determine ligand atomic index
    if (self._FNs['prmtop']['R'] is not None) and \
       (self._FNs['prmtop']['RL'] is not None):
      import AlGDock.IO
      IO_prmtop = AlGDock.IO.prmtop()
      prmtop_R = IO_prmtop.read(self._FNs['prmtop']['R'])
      prmtop_RL = IO_prmtop.read(self._FNs['prmtop']['RL'])
      ligand_ind = [ind for ind in range(len(prmtop_RL['RESIDUE_LABEL']))
        if prmtop_RL['RESIDUE_LABEL'][ind] not in prmtop_R['RESIDUE_LABEL']]
      if len(ligand_ind)==0:
        raise Exception('Ligand not found in complex prmtop')
      elif len(ligand_ind) > 1:
        print '  possible ligand residue labels: '+\
          ', '.join([prmtop_RL['RESIDUE_LABEL'][ind] for ind in ligand_ind])
      print 'ligand residue name: ' + \
        prmtop_RL['RESIDUE_LABEL'][ligand_ind[-1]].strip()
      self._ligand_first_atom = prmtop_RL['RESIDUE_POINTER'][ligand_ind[-1]] - 1
    else:
      self._ligand_first_atom = 0
      if do_dock:
        raise Exception('Missing AMBER prmtop files for receptor')
      else:
        print 'Missing AMBER prmtop files for receptor'

    # Read the reference ligand and receptor coordinates
    import AlGDock.IO
    IO_crd = AlGDock.IO.crd()
    if self._FNs['inpcrd']['R'] is not None:
      if os.path.isfile(self._FNs['inpcrd']['L']):
        lig_crd = IO_crd.read(self._FNs['inpcrd']['L'], multiplier=0.1)
      self.confs['receptor'] = IO_crd.read(\
        self._FNs['inpcrd']['R'], multiplier=0.1)
    elif self._FNs['inpcrd']['RL'] is not None:
      complex_crd = IO_crd.read(self._FNs['inpcrd']['RL'], multiplier=0.1)
      lig_crd = complex_crd[self._ligand_first_atom:self._ligand_first_atom + \
        self._ligand_natoms,:]
      self.confs['receptor'] = np.vstack(\
        (complex_crd[:self._ligand_first_atom,:],\
         complex_crd[self._ligand_first_atom + self._ligand_natoms:,:]))
    elif self._FNs['inpcrd']['L'] is not None:
      self.confs['receptor'] = None
      if os.path.isfile(self._FNs['inpcrd']['L']):
        lig_crd = IO_crd.read(self._FNs['inpcrd']['L'], multiplier=0.1)
    else:
      lig_crd = None

    if lig_crd is not None:
      self.confs['ligand'] = lig_crd[self.molecule.inv_prmtop_atom_order,:]
      self.universe.setConfiguration(\
        Configuration(self.universe,self.confs['ligand']))

    if self.params['dock']['rmsd'] is not False:
      if self.params['dock']['rmsd'] is True:
        if lig_crd is not None:
          rmsd_crd = lig_crd[self.molecule.inv_prmtop_atom_order,:]
        else:
          raise Exception('Reference structure for rmsd calculations unknown')
      else:
        rmsd_crd = IO_crd.read(self.params['dock']['rmsd'], \
          natoms=self.universe.numberOfAtoms(), multiplier=0.1)
        rmsd_crd = rmsd_crd[self.molecule.inv_prmtop_atom_order,:]
      self.confs['rmsd'] = rmsd_crd

    # Locate programs for postprocessing
    all_phases = self.params['dock']['phases'] + self.params['cool']['phases']
    self._load_programs(all_phases)

    # Determine APBS grid spacing
    if 'APBS_PBSA' in self.params['dock']['phases'] or \
       'APBS_PBSA' in self.params['cool']['phases']:
      self._get_APBS_grid_spacing()

    # Determines receptor electrostatic size
    if np.array([p.find('ALPB')>-1 for p in all_phases]).any():
      self.elsize = self._get_elsize()

    # If configurations are being rescored, start with a docked structure
    (confs, Es) = self._get_confs_to_rescore(site=False, minimize=False)
    if len(confs)>0:
      self.universe.setConfiguration(Configuration(self.universe,confs[-1]))

    # Samplers may accept the following options:
    # steps - number of MD steps
    # T - temperature in K
    # delta_t - MD time step
    # normalize - normalizes configurations
    # adapt - uses an adaptive time step

    self.sampler = {}
    # Uses cython class
    # from SmartDarting import SmartDartingIntegrator # @UnresolvedImport
    # Uses python class
    from AlGDock.Integrators.SmartDarting.SmartDarting \
      import SmartDartingIntegrator # @UnresolvedImport
    self.sampler['cool_SmartDarting'] = SmartDartingIntegrator(\
      self.universe, self.molecule, False)
    self.sampler['dock_SmartDarting'] = SmartDartingIntegrator(\
      self.universe, self.molecule, True)
    from AlGDock.Integrators.ExternalMC.ExternalMC import ExternalMCIntegrator
    self.sampler['ExternalMC'] = ExternalMCIntegrator(\
      self.universe, self.molecule, step_size=0.25*MMTK.Units.Ang)

    for p in ['cool', 'dock']:
      if self.params[p]['sampler'] == 'MixedHMC':
        from AlGDock.Integrators.CDHMC import CDHMC
        from AlGDock.Integrators.HamiltonianMonteCarlo.HamiltonianMonteCarlo \
          import HamiltonianMonteCarloIntegrator
        self.mixed_samplers = []
        self.mixed_samplers.append(CDHMC.CDHMCIntegrator(self.universe, \
          os.path.dirname(self._FNs['ligand_database']), \
          os.path.dirname(self._FNs['forcefield'])))
        self.mixed_samplers.append(HamiltonianMonteCarloIntegrator(self.universe))
        self.sampler[p] = self._mixed_sampler2   #EU END
#        from AlGDock.Integrators.CDHMC import CDHMC
#        from AlGDock.Integrators.MixedHMC.MixedHMC import MixedHMCIntegrator
#        CDIntegrator = CDHMC.CDHMCIntegrator(self.universe, \
#          os.path.dirname(self._FNs['mol2']['L']), \
#          os.path.dirname(self._FNs['forcefield']))
#        self.sampler[p] = MixedHMCIntegrator(self.universe, CDIntegrator, \
#          fraction_CD=self.params[p]['fraction_CD'], \
#          CD_steps_per_trial=self.params[p]['CD_steps_per_trial'], \
#          delta_t_CD=self.params[p]['delta_t_CD'])
      elif self.params[p]['sampler'] == 'HMC':
        from AlGDock.Integrators.HamiltonianMonteCarlo.HamiltonianMonteCarlo \
          import HamiltonianMonteCarloIntegrator
        self.sampler[p] = HamiltonianMonteCarloIntegrator(self.universe)
      elif self.params[p]['sampler'] == 'NUTS':
        from NUTS import NUTSIntegrator # @UnresolvedImport
        self.sampler[p] = NUTSIntegrator(self.universe)
      elif self.params[p]['sampler'] == 'VV':
        from AlGDock.Integrators.VelocityVerlet.VelocityVerlet \
          import VelocityVerletIntegrator
        self.sampler[p] = VelocityVerletIntegrator(self.universe)
      else:
        raise Exception('Unrecognized sampler!')

    # Load progress
    self._postprocess(readOnly=True)
    self.calc_f_L(readOnly=True)
    self.calc_f_RL(readOnly=True)

    if self._random_seed>0:
      np.random.seed(self._random_seed)

  def _run(self, run_type):
    self.start_times['run'] = time.time()
    self._run_type = run_type
    if run_type=='configuration_energies' or \
       run_type=='minimized_configuration_energies':
      self.configuration_energies(\
        minimize = (run_type=='minimized_configuration_energies'), \
        max_confs = 50)
    elif run_type=='store_params':
      self._save('cool', keys=['progress'])
      self._save('dock', keys=['progress'])
    elif run_type=='initial_cool':
      self.initial_cool()
    elif run_type=='cool': # Sample the cooling process
      self.sim_process('cool')
      self._postprocess([('cool',-1,-1,'L')])
      self.calc_f_L()
    elif run_type=='initial_dock':
      self.initial_dock()
    elif run_type=='dock': # Sample the docking process
      self.sim_process('dock')
      self._postprocess()
      self.calc_f_RL()
    elif run_type=='timed': # Timed replica exchange sampling
      cool_complete = self.sim_process('cool')
      if cool_complete:
        pp_complete = self._postprocess([('cool',-1,-1,'L')])
        if pp_complete:
          self.calc_f_L()
          dock_complete = self.sim_process('dock')
          if dock_complete:
            pp_complete = self._postprocess()
            if pp_complete:
              self.calc_f_RL()
    elif run_type=='postprocess': # Postprocessing
      self._postprocess()
    elif run_type=='redo_postprocess':
      self._postprocess(redo_dock=True)
    elif (run_type=='free_energies') or (run_type=='redo_free_energies'):
      self.calc_f_L(redo=(run_type=='redo_free_energies'))
      self.calc_f_RL(redo=(run_type=='redo_free_energies'))
    elif run_type=='all':
      self.sim_process('cool')
      self._postprocess([('cool',-1,-1,'L')])
      self.calc_f_L()
      self.sim_process('dock')
      self._postprocess()
      self.calc_f_RL()
    elif run_type=='render_docked':
      view_args = {'axes_off':True, 'size':[1008,1008], 'scale_by':0.80, \
                   'render':'TachyonInternal'}
      if hasattr(self, '_view_args_rotate_matrix'):
        view_args['rotate_matrix'] = getattr(self, '_view_args_rotate_matrix')
      self.show_samples(show_ref_ligand=True, show_starting_pose=True, \
        show_receptor=True, save_image=True, execute=True, quit=True, \
        view_args=view_args)
    elif run_type=='render_intermediates':
      view_args = {'axes_off':True, 'size':[1008,1008], 'scale_by':0.80, \
                   'render':'TachyonInternal'}
      if hasattr(self, '_view_args_rotate_matrix'):
        view_args['rotate_matrix'] = getattr(self, '_view_args_rotate_matrix')
      self.render_intermediates(\
        movie_name=os.path.join(self.dir['dock'],'dock-intermediates.gif'), \
        view_args=view_args)
      self.render_intermediates(nframes=8, view_args=view_args)
    elif run_type=='clear_intermediates':
      for process in ['cool','dock']:
        print 'Clearing intermediates for '+process
        for state_ind in range(1,len(self.confs[process]['samples'])-1):
          for cycle_ind in range(len(self.confs[process]['samples'][state_ind])):
            self.confs[process]['samples'][state_ind][cycle_ind] = []
        self._save(process)
    if run_type is not None:
      print "\nElapsed time for execution of %s: %s"%(run_type,
        HMStime(time.time()-self.start_times['run']))

  ###########
  # Cooling #
  ###########
  def initial_cool(self, warm=True):
    """
    Warms the ligand from self.T_SIMMIN to self.T_HIGH, or
    cools the ligand from self.T_HIGH to self.T_SIMMIN
    
    Intermediate thermodynamic states are chosen such that
    thermodynamic length intervals are approximately constant.
    Configurations from each state are subsampled to seed the next simulation.
    """

    if (len(self.cool_protocol)>0) and (self.cool_protocol[-1]['crossed']):
      return # Initial cooling is already complete
    
    self._set_lock('cool')
    self.start_times['cool'] = time.time()
    self.start_times['cool_save'] = time.time()

    direction_name = 'warm' if warm else 'cool'
    if self.cool_protocol==[]:
      self.tee("\n>>> Initial %sing, starting at "%direction_name + \
        time.strftime("%a, %d %b %Y %H:%M:%S", time.localtime()) + "\n")

      # Set up the force field
      lambda_o = self._lambda(1.0 if warm else 0., 'cool', site=False)
      self.cool_protocol = [lambda_o]
      self._set_universe_evaluator(lambda_o)
      
      # Get starting configurations
      seeds = self._get_confs_to_rescore(site=False, minimize=True)[0]
      # initializes smart darting for cooling
      # and sets the universe to the lowest energy configuration
      if self.params['cool']['darts_per_seed']>0:
        self.tee(self.sampler['cool_SmartDarting'].set_confs(seeds))
        self.confs['cool']['SmartDarting'] = \
          self.sampler['cool_SmartDarting'].confs
      elif len(seeds)>0:
        self.universe.setConfiguration(Configuration(self.universe,seeds[-1]))
      self.confs['cool']['starting_poses'] = seeds
      
      # Ramp the temperature from 0 to the desired starting temperature using HMC
      self._ramp_T(lambda_o['T'], normalize=True)

      # Run at starting temperature
      self.start_times['cool_state'] = time.time()
      seeds = [np.copy(self.universe.configuration().array) \
        for n in range(self.params['cool']['seeds_per_state'])]
      (confs, DeltaEs, lambda_o['delta_t'], sampler_metrics) = \
        self._initial_sim_state(seeds, 'cool', lambda_o)
      E = self._energyTerms(confs, process='cool')
      self.confs['cool']['replicas'] = [confs[np.random.randint(len(confs))]]
      self.confs['cool']['samples'] = [[confs]]
      self.cool_Es = [[E]]

      self.tee("  at %d K in %s: %s"%(lambda_o['T'],
        HMStime(time.time()-self.start_times['cool_state']), sampler_metrics))
      self.tee("    dt=%.2f fs; tL_tensor=%.3e"%(\
        lambda_o['delta_t']*1000., self._tL_tensor(E,lambda_o,process='cool')))
    else:
      self.tee("\n>>> Initial %sing, continuing at "%direction_name + \
        time.strftime("%a, %d %b %Y %H:%M:%S", time.localtime()))
      lambda_o = self.cool_protocol[-1]
      confs = self.confs['cool']['samples'][-1][0]
      E = self.cool_Es[-1][0]

    if self.params['cool']['darts_per_seed']>0:
      self.confs['cool']['SmartDarting'] += confs

    # Main loop for initial cooling:
    # choose new temperature, randomly select seeds, simulate
    rejectStage = 0
    while (not self.cool_protocol[-1]['crossed']):
      # Choose new temperature
      lambda_n = self._next_cool_state(E = E, lambda_o = lambda_o, \
        pow = rejectStage, warm = warm)
      self.cool_protocol.append(lambda_n)

      # Randomly select seeds for new trajectory
      u_o = self._u_kln([E],[lambda_o])
      u_n = self._u_kln([E],[lambda_n])
      du = u_n-u_o
      weights = np.exp(-du+min(du))
      seedIndicies = np.random.choice(len(u_o), \
        size = self.params['cool']['seeds_per_state'], \
        p = weights/sum(weights))
      seeds = [np.copy(confs[s]) for s in seedIndicies]
      
      # Store old data
      confs_o = confs
      E_o = E
      
      # Simulate
      self.start_times['cool_state'] = time.time()
      self._set_universe_evaluator(lambda_n)
      if self.params['cool']['darts_per_seed']>0:
        self.tee(self.sampler['cool_SmartDarting'].set_confs(\
          self.confs['cool']['SmartDarting']))
        self.confs['cool']['SmartDarting'] = self.sampler['cool_SmartDarting'].confs
      (confs, DeltaEs, lambda_n['delta_t'], sampler_metrics) = \
        self._initial_sim_state(seeds, 'cool', lambda_n)

      if self.params['cool']['darts_per_seed']>0:
        self.confs['cool']['SmartDarting'] += confs

      # Get state energies
      E = self._energyTerms(confs, process='cool')

      # Estimate the mean replica exchange acceptance rate
      # between the previous and new state
      (u_kln,N_k) = self._u_kln([[E_o],[E]], self.cool_protocol[-2:])
      N = min(N_k)
      acc = np.exp(-u_kln[0,1,:N]-u_kln[1,0,:N]+u_kln[0,0,:N]+u_kln[1,1,:N])
      mean_acc = np.mean(np.minimum(acc,np.ones(acc.shape)))

      self.tee("  at %d K in %s: %s"%(\
        lambda_n['T'], HMStime(time.time()-self.start_times['cool_state']), sampler_metrics))
      self.tee("    dt=%.2f fs; tL_tensor=%.2e; <acc>=%.2f"%(\
        lambda_n['delta_t']*1000., \
        self._tL_tensor(E,lambda_o,process='cool'), mean_acc))

      if (mean_acc<self.params['cool']['min_repX_acc']) and \
          (self.params['cool']['protocol']=='Adaptive'):
        # If the acceptance probability is too low,
        # reject the state and restart
        self.cool_protocol.pop()
        confs = confs_o
        E = E_o
        rejectStage += 1
        self.tee("  rejected new state with low acceptance rate")
      elif (len(self.cool_protocol)>2) and \
          (mean_acc>0.99) and (not lambda_n['crossed']) and \
          (self.params['cool']['protocol'] == 'Adaptive'):
        # If the acceptance probability is too high,
        # reject the previous state and restart
        self.confs['cool']['replicas'][-1] = confs[np.random.randint(len(confs))]
        self.cool_protocol.pop()
        self.cool_protocol[-1] = copy.deepcopy(lambda_n)
        rejectStage -= 1
        lambda_o = lambda_n
        self.tee("  rejected previous state with high acceptance rate")
      else:
        # Store data and continue with initialization
        self.confs['cool']['replicas'].append(confs[np.random.randint(len(confs))])
        self.confs['cool']['samples'].append([confs])
        self.cool_Es.append([E])
        self.cool_protocol[-1] = copy.deepcopy(lambda_n)
        rejectStage = 0
        lambda_o = lambda_n

      # Special tasks after the last stage
      if self.cool_protocol[-1]['crossed']:
        # For warming, reverse protocol and energies
        if warm:
          self.tee("  reversing replicas, samples, and protocol")
          self.confs['cool']['replicas'].reverse()
          self.confs['cool']['samples'].reverse()
          self.cool_Es.reverse()
          self.cool_protocol.reverse()
          self.cool_protocol[0]['crossed'] = False
          self.cool_protocol[-1]['crossed'] = True

        if (not self.params['cool']['keep_intermediate']):
          for k in range(1,len(self.cool_protocol)-1):
            self.confs['cool']['samples'][k] = []
            
        self._cool_cycle += 1

      # Save progress every 5 minutes
      if ((time.time()-self.start_times['cool_save'])>5*60):
        self._save('cool')
        self.start_times['cool_save'] = time.time()
        saved = True
      else:
        saved = False

      if self._run_type=='timed':
        remaining_time = self.timings['max']*60 - \
          (time.time()-self.start_times['run'])
        if remaining_time<0:
          if not saved:
            self._save('cool')
            self.tee("")
          self.tee("  no time remaining for initial cool")
          self._clear_lock('cool')
          return False

    # Save data
    if not saved:
      self._save('cool')
      self.tee("")

    self.tee("Elapsed time for initial %sing of "%direction_name + \
      "%d states: "%len(self.cool_protocol) + \
      HMStime(time.time()-self.start_times['cool']))
    self._clear_lock('cool')
    self.sampler['cool_SmartDarting'].confs = []
    return True

  def calc_f_L(self, readOnly=False, do_solvation=True, redo=False):
    """
    Calculates ligand-specific free energies:
    1. reduced free energy of cooling the ligand
       from self.T_HIGH to self.T_SIMMIN
    2. solvation free energy of the ligand using single-step
       free energy perturbation
    redo does not do anything now; it is an option for debugging
    """
    # Initialize variables as empty lists or by loading data
    f_L_FN = os.path.join(self.dir['cool'],'f_L.pkl.gz')
    dat = self._load_pkl_gz(f_L_FN)
    if dat is not None:
      (self.stats_L, self.f_L) = dat
    else:
      self.stats_L = dict(\
        [(item,[]) for item in ['equilibrated_cycle','mean_acc']])
      self.stats_L['protocol'] = self.cool_protocol
      self.f_L = dict([(key,[]) for key in ['cool_MBAR'] + \
        [phase+'_solv' for phase in self.params['cool']['phases']]])
    if readOnly or self.cool_protocol==[]:
      return

    K = len(self.cool_protocol)

    # Make sure all the energies are available
    for c in range(self._cool_cycle):
      if len(self.cool_Es[-1][c].keys())==0:
        self.tee("  skipping the cooling free energy calculation")
        return

    start_string = "\n>>> Ligand free energy calculations, starting at " + \
      time.strftime("%a, %d %b %Y %H:%M:%S", time.localtime()) + "\n"
    self.start_times['free energy'] = time.time()

    # Store stats_L internal energies
    self.stats_L['u_K_sampled'] = \
      [self._u_kln([self.cool_Es[-1][c]],[self.cool_protocol[-1]]) \
        for c in range(self._cool_cycle)]
    self.stats_L['u_KK'] = \
      [np.sum([self._u_kln([self.cool_Es[k][c]],[self.cool_protocol[k]]) \
        for k in range(len(self.cool_protocol))],0) \
          for c in range(self._cool_cycle)]

    self.stats_L['equilibrated_cycle'] = self._get_equilibrated_cycle('cool')
    
    # Calculate cooling free energies that have not already been calculated,
    # in units of RT
    updated = False
    for c in range(len(self.f_L['cool_MBAR']), self._cool_cycle):
      if not updated:
        self._set_lock('cool')
        if do_solvation:
          self.tee(start_string)
        updated = True
      
      fromCycle = self.stats_L['equilibrated_cycle'][c]
      toCycle = c + 1

      # Cooling free energy
      cool_Es = []
      for cool_Es_state in self.cool_Es:
        cool_Es.append(cool_Es_state[fromCycle:toCycle])
      (u_kln,N_k) = self._u_kln(cool_Es,self.cool_protocol)
      MBAR = self._run_MBAR(u_kln,N_k)[0]
      self.f_L['cool_MBAR'].append(MBAR)

      # Average acceptance probabilities
      cool_mean_acc = np.zeros(K-1)
      for k in range(0, K-1):
        (u_kln, N_k) = self._u_kln(cool_Es[k:k+2],self.cool_protocol[k:k+2])
        N = min(N_k)
        acc = np.exp(-u_kln[0,1,:N]-u_kln[1,0,:N]+u_kln[0,0,:N]+u_kln[1,1,:N])
        cool_mean_acc[k] = np.mean(np.minimum(acc,np.ones(acc.shape)))
      self.stats_L['mean_acc'].append(cool_mean_acc)

      self.tee("  calculated cooling free energy of %.2f RT "%(\
                  self.f_L['cool_MBAR'][-1][-1])+\
               "using cycles %d to %d"%(fromCycle, c))

    if not do_solvation:
      if updated:
        if not self._run_type=='timed':
          self._write_pkl_gz(f_L_FN, (self.stats_L,self.f_L), quiet=True)
        self._clear_lock('cool')
      return True

    # Make sure postprocessing is complete
    pp_complete = self._postprocess([('cool',-1,-1,'L')])
    if not pp_complete:
      return False

    # Store stats_L internal energies
    for phase in self.params['cool']['phases']:
      self.stats_L['u_K_'+phase] = \
        [self.cool_Es[-1][c]['L'+phase][:,-1]/self.RT_SIMMIN \
          for c in range(self._cool_cycle)]

    # Get predicted pose (not really needed for cooling)
    (self.stats_L['pose_inds'], self.stats_L['scores']) = \
      self._get_pose_prediction('cool', self.stats_L['equilibrated_cycle'][-1])

    # Calculate solvation free energies that have not already been calculated,
    # in units of RT
    for phase in self.params['cool']['phases']:
      if not phase+'_solv' in self.f_L:
        self.f_L[phase+'_solv'] = []
      if not 'mean_'+phase in self.f_L:
        self.f_L['mean_'+phase] = []

      for c in range(len(self.f_L[phase+'_solv']), self._cool_cycle):
        if not updated:
          self._set_lock('cool')
          self.tee(start_string)
          updated = True

        fromCycle = self.stats_L['equilibrated_cycle'][c]
        toCycle = c + 1
        
        if not ('L'+phase) in self.cool_Es[-1][c].keys():
          raise Exception('L%s energies not found in cycle %d'%(phase, c))
        
        # Arbitrarily, solvation is the
        # 'forward' direction and desolvation the 'reverse'
        u_L = np.concatenate([self.cool_Es[-1][n]['L'+phase] \
          for n in range(fromCycle,toCycle)])/self.RT_TARGET
        u_sampled = np.concatenate(\
          [self._u_kln([self.cool_Es[-1][c]],[self.cool_protocol[-1]]) \
            for c in range(fromCycle,toCycle)])
        du_F = (u_L[:,-1] - u_sampled)
        min_du_F = min(du_F)
        w_L = np.exp(-du_F+min_du_F)
        f_L_solv = -np.log(np.mean(w_L)) + min_du_F
        mean_u_phase = np.sum(u_L[:,-1]*w_L)/np.sum(w_L)

        self.f_L[phase+'_solv'].append(f_L_solv)
        self.f_L['mean_'+phase].append(mean_u_phase)
        self.tee("  calculated " + phase + " solvation free energy of " + \
                 "%.5g RT "%(f_L_solv) + \
                 "using cycles %d to %d"%(fromCycle, toCycle-1))

    if updated:
      self._write_pkl_gz(f_L_FN, (self.stats_L,self.f_L))
      self.tee("\nElapsed time for free energy calculation: " + \
        HMStime(time.time()-self.start_times['free energy']))
      self._clear_lock('cool')
    return True

  ###########
  # Docking #
  ###########
  def random_dock(self):
    """
      Randomly places the ligand into the receptor and evaluates energies
      
      The first state of docking is sampled by randomly placing configurations
      from the high temperature ligand simulation into the binding site.
    """
    # Select samples from the high T unbound state
    E_MM = []
    E_OBC = []
    confs = []
    for k in range(1,len(self.cool_Es[0])):
      E_MM += list(self.cool_Es[0][k]['MM'])
      if ('OBC' in self.cool_Es[0][k].keys()):
        E_OBC += list(self.cool_Es[0][k]['OBC'])
      confs += list(self.confs['cool']['samples'][0][k])

    random_dock_inds = np.array(np.linspace(0,len(E_MM), \
      self.params['dock']['seeds_per_state'],endpoint=False),dtype=int)
    cool0_Es_MM = [E_MM[ind] for ind in random_dock_inds]
    cool0_Es_OBC = []
    if E_OBC!=[]:
      cool0_Es_OBC = [E_OBC[ind] for ind in random_dock_inds]
    cool0_confs = [confs[ind] for ind in random_dock_inds]

    # Do the random docking
    lambda_o = self._lambda(0.0, 'dock')
    lambda_o['delta_t'] = 1.*self.cool_protocol[0]['delta_t']
    self.dock_protocol = [lambda_o]

    # Set up the force field with full interaction grids
    self._set_universe_evaluator(self._lambda(1.0, 'dock'))

    # Either loads or generates the random translations and rotations for the first state of docking
    if not (hasattr(self,'_random_trans') and hasattr(self,'_random_rotT')):
      self._max_n_trans = 10000
      # Default density of points is 50 per nm**3
      self._n_trans = max(min(np.int(np.ceil(self._forceFields['site'].volume*self.params['dock']['site_density'])),self._max_n_trans),5)
      self._random_trans = np.ndarray((self._max_n_trans), dtype=Vector)
      for ind in range(self._max_n_trans):
        self._random_trans[ind] = Vector(self._forceFields['site'].randomPoint())
      self._max_n_rot = 100
      self._n_rot = 100
      self._random_rotT = np.ndarray((self._max_n_rot,3,3))
      from AlGDock.Integrators.ExternalMC.ExternalMC import random_rotate
      for ind in range(self._max_n_rot):
        self._random_rotT[ind,:,:] = np.transpose(random_rotate())
    else:
      self._max_n_trans = self._random_trans.shape[0]
      self._n_rot = self._random_rotT.shape[0]

    # Get interaction energies.
    # Loop over configurations, random rotations, and random translations
    E = {}
    for term in (['MM','site']+self._scalables):
      # Large array creation may cause MemoryError
      E[term] = np.zeros((self.params['dock']['seeds_per_state'], \
        self._max_n_rot,self._n_trans))
    self.tee("  allocated memory for interaction energies")

    converged = False
    n_trans_o = 0
    n_trans_n = self._n_trans
    while not converged:
      for c in range(self.params['dock']['seeds_per_state']):
        E['MM'][c,:,:] = cool0_Es_MM[c]
        if cool0_Es_OBC!=[]:
          E['OBC'][c,:,:] = cool0_Es_OBC[c]
        for i_rot in range(self._n_rot):
          conf_rot = Configuration(self.universe,\
            np.dot(cool0_confs[c], self._random_rotT[i_rot,:,:]))
          for i_trans in range(n_trans_o, n_trans_n):
            self.universe.setConfiguration(conf_rot)
            self.universe.translateTo(self._random_trans[i_trans])
            eT = self.universe.energyTerms()
            for (key,value) in eT.iteritems():
              if key!='electrostatic': # For some reason, MMTK double-counts electrostatic energies
                E[term_map[key]][c,i_rot,i_trans] += value
      E_c = {}
      for term in E.keys():
        # Large array creation may cause MemoryError
        E_c[term] = np.ravel(E[term][:,:self._n_rot,:n_trans_n])
      self.tee("  allocated memory for %d translations"%n_trans_n)
      (u_kln,N_k) = self._u_kln([E_c],\
        [lambda_o,self._next_dock_state(E=E_c, lambda_o=lambda_o, undock=False)])
      du = u_kln[0,1,:] - u_kln[0,0,:]
      bootstrap_reps = 50
      f_grid0 = np.zeros(bootstrap_reps)
      for b in range(bootstrap_reps):
        du_b = du[np.random.randint(0, len(du), len(du))]
        f_grid0[b] = -np.log(np.exp(-du_b+min(du_b)).mean()) + min(du_b)
      f_grid0_std = f_grid0.std()
      converged = f_grid0_std<0.1
      if not converged:
        self.tee("  with %s translations "%n_trans_n + \
                 "the predicted free energy difference is %.5g (%.5g)"%(\
                 f_grid0.mean(),f_grid0_std))
        if n_trans_n == self._max_n_trans:
          break
        n_trans_o = n_trans_n
        n_trans_n = min(n_trans_n + 25, self._max_n_trans)
        for term in (['MM','site']+self._scalables):
          # Large array creation may cause MemoryError
          E[term] = np.dstack((E[term], \
            np.zeros((self.params['dock']['seeds_per_state'],\
              self._max_n_rot,25))))

    if self._n_trans != n_trans_n:
      self._n_trans = n_trans_n
      
    self.tee("  %d ligand configurations "%len(cool0_Es_MM) + \
             "were randomly docked into the binding site using "+ \
             "%d translations and %d rotations "%(n_trans_n,self._n_rot))
    self.tee("  the predicted free energy difference between the" + \
             " first and second docking states is " + \
             "%.5g (%.5g)"%(f_grid0.mean(),f_grid0_std))

    self.start_times['ravel'] = time.time()
    for term in E.keys():
      E[term] = np.ravel(E[term][:,:self._n_rot,:self._n_trans])
    self.tee("  raveled energy terms in " + \
      HMStime(time.time()-self.start_times['ravel']))

    return (cool0_confs, E)

  def initial_dock(self, randomOnly=False):
    """
      Docks the ligand into the receptor
      
      Intermediate thermodynamic states are chosen such that
      thermodynamic length intervals are approximately constant.
      Configurations from each state are subsampled to seed the next simulation.
    """
    
    if (len(self.dock_protocol)>0) and (self.dock_protocol[-1]['crossed']):
      return # Initial docking already complete

    self._set_lock('dock')
    self.start_times['initial_dock'] = time.time()
    self.start_times['dock_save'] = time.time()
    
    if self.dock_protocol==[]:
      self.tee("\n>>> Initial docking, starting at " + \
        time.strftime("%a, %d %b %Y %H:%M:%S", time.localtime()) + "\n")
      undock = True
      if undock:
        lambda_o = self._lambda(1.0, 'dock')
        self.dock_protocol = [lambda_o]
        self._set_universe_evaluator(lambda_o)
        
        if (self.params['dock']['pose'] == -1):
          seeds = self._get_confs_to_rescore(site=True, minimize=True)[0]
          self.confs['dock']['starting_poses'] = seeds
        else:
          # For pose BPMF, starting_poses is defined in _set_universe_evaluator
          seeds = self.confs['dock']['starting_poses']

        if seeds==[]:
          undock = False
        else:
          # initializes smart darting for docking and sets the universe
          # to the lowest energy configuration
          if self.params['dock']['darts_per_seed']>0:
            self.tee(self.sampler['dock_SmartDarting'].set_confs(seeds))
            self.confs['dock']['SmartDarting'] = \
              self.sampler['dock_SmartDarting'].confs
          elif len(seeds)>0:
            self.universe.setConfiguration(\
              Configuration(self.universe,np.copy(seeds[-1])))

          attempts = 0
          DeltaEs = np.array([0.])
          while np.std(DeltaEs)<1E-7:
            # Ramp up the temperature using HMC
            self._ramp_T(self.T_SIMMIN, normalize=False)

            seeds = [np.copy(self.universe.configuration().array) \
              for n in range(self.params['dock']['seeds_per_state'])]
            # Simulate
            (confs, DeltaEs, lambda_o['delta_t'], sampler_metrics) = \
              self._initial_sim_state(seeds, 'dock', lambda_o)
            
            attempts += 1
            if attempts == 5:
              self._store_infinite_f_RL()
              raise Exception('Unable to ramp temperature')

          # Get state energies
          E = self._energyTerms(confs)
          self.confs['dock']['replicas'] = [confs[np.random.randint(len(confs))]]
          self.confs['dock']['samples'] = [[confs]]
          self.dock_Es = [[E]]

          self.tee("\n  at a=%.3e in %s: %s"%(\
            lambda_o['a'], HMStime(time.time()-self.start_times['initial_dock']), sampler_metrics))
          self.tee("    dt=%.2f fs, tL_tensor=%.3e"%(\
            lambda_o['delta_t']*1000., \
            self._tL_tensor(E,lambda_o)))
    
      if not undock:
        # Select samples from the high T unbound state and ensure there are enough
        confs_HT = []
        for k in range(1,len(self.cool_Es[0])):
          confs_HT += list(self.confs['cool']['samples'][0][k])
        while len(confs_HT)<self.params['dock']['seeds_per_state']:
          self.tee("More samples from high temperature ligand simulation needed")
          self._clear_lock('dock')
          self._replica_exchange('cool')
          self._set_lock('dock')
          confs_HT = []
          for k in range(1,len(self.cool_Es[0])):
            confs_HT += list(self.confs['cool']['samples'][0][k])
        confs_HT = confs_HT[:self.params['dock']['seeds_per_state']]
        
        (confs, E) = self.random_dock()
        self.tee("  random docking complete in " + \
                 HMStime(time.time()-self.start_times['initial_dock']))
        if randomOnly:
          self._clear_lock('dock')
          return
    else:
      # Continuing from a previous docking instance
      undock = self.dock_protocol[0]['a']>self.dock_protocol[1]['a']
      self.tee("\n>>> Initial %sdocking, "%({True:'un',False:''}[undock]) + \
        "continuing at " + \
        time.strftime("%a, %d %b %Y %H:%M:%S", time.localtime()))
      confs = self.confs['dock']['samples'][-1][0]
      E = self.dock_Es[-1][0]

    if self.params['dock']['darts_per_seed']>0:
      self.confs['dock']['SmartDarting'] += confs

    lambda_o = self.dock_protocol[-1]

    # Main loop for initial docking:
    # choose new thermodynamic variables,
    # randomly select seeds,
    # simulate
    rejectStage = 0
    while (not self.dock_protocol[-1]['crossed']):
      # Determine next value of the protocol
      lambda_n = self._next_dock_state(E = E, lambda_o = lambda_o, \
          pow = rejectStage, undock = undock)
      self.dock_protocol.append(lambda_n)
      if len(self.dock_protocol)>1000:
        self._clear('dock')
        self._save('dock')
        self._store_infinite_f_RL()
        raise Exception('Too many replicas!')
      if abs(rejectStage)>20:
        self._clear('dock')
        self._save('dock')
        self._store_infinite_f_RL()
        raise Exception('Too many consecutive rejected stages!')

      # Randomly select seeds for new trajectory
      u_o = self._u_kln([E],[lambda_o])
      u_n = self._u_kln([E],[lambda_n])
      du = u_n-u_o
      weights = np.exp(-du+min(du))
      seedIndicies = np.random.choice(len(u_o), \
        size = self.params['dock']['seeds_per_state'], \
        p=weights/sum(weights))

      if (not undock) and (self.params['dock']['pose'] == -1) \
          and (len(self.dock_protocol)==2):
        # Cooling state 0 configurations, randomly oriented
        # Use the lowest energy configuration
        # in the first docking state for replica exchange
        ind = np.argmin(u_n)
        (c,i_rot,i_trans) = np.unravel_index(ind, \
          (self.params['dock']['seeds_per_state'], self._n_rot, self._n_trans))
        repX_conf = np.add(np.dot(confs[c], self._random_rotT[i_rot,:,:]),\
                           self._random_trans[i_trans].array)
        self.confs['dock']['replicas'] = [repX_conf]
        self.confs['dock']['samples'] = [[repX_conf]]
        self.dock_Es = [[dict([(key,np.array([val[ind]])) \
          for (key,val) in E.iteritems()])]]
        seeds = []
        for ind in seedIndicies:
          (c,i_rot,i_trans) = np.unravel_index(ind, \
            (self.params['dock']['seeds_per_state'], self._n_rot, self._n_trans))
          seeds.append(np.add(np.dot(confs[c], self._random_rotT[i_rot,:,:]), \
            self._random_trans[i_trans].array))
        confs = None
        E = {}
      else: # Seeds from last state
        seeds = [np.copy(confs[ind]) for ind in seedIndicies]
      self.confs['dock']['seeds'] = seeds

      # Store old data
      confs_o = confs
      E_o = E

      # Simulate
      self.start_times['dock_state'] = time.time()
      self._set_universe_evaluator(lambda_n)
      if self.params['dock']['darts_per_seed']>0  and lambda_n['a']>0.1:
        self.tee(self.sampler['dock_SmartDarting'].set_confs(\
          self.confs['dock']['SmartDarting']))
        self.confs['dock']['SmartDarting'] = self.sampler['dock_SmartDarting'].confs
      (confs, DeltaEs, lambda_n['delta_t'], sampler_metrics) = \
        self._initial_sim_state(seeds, 'dock', lambda_n)
      if np.std(DeltaEs)<1E-7:
        self._store_infinite_f_RL()
        raise Exception('Unable to initialize simulation')

      if self.params['dock']['darts_per_seed']>0:
        self.confs['dock']['SmartDarting'] += confs

      # Get state energies
      E = self._energyTerms(confs)

      # Estimate the mean replica exchange acceptance rate
      # between the previous and new state
      self.tee("  at a=%.3e in %s: %s"%(\
        lambda_n['a'], \
        HMStime(time.time()-self.start_times['dock_state']), sampler_metrics))

      if E_o!={}:
        (u_kln,N_k) = self._u_kln([[E_o],[E]], self.dock_protocol[-2:])
        N = min(N_k)
        acc = np.exp(-u_kln[0,1,:N]-u_kln[1,0,:N]+u_kln[0,0,:N]+u_kln[1,1,:N])
        mean_acc = np.mean(np.minimum(acc,np.ones(acc.shape)))

        self.tee("    dt=%.2f fs; tL_tensor=%.3e; <acc>=%.2f"%(\
          lambda_n['delta_t']*1000., self._tL_tensor(E,lambda_o), mean_acc))
      else:
        self.tee("    dt=%.2f fs; tL_tensor=%.3e"%(\
          lambda_n['delta_t']*1000., self._tL_tensor(E,lambda_o)))

      # Decide whether to keep the state
      if len(self.dock_protocol)>(1+(not undock)):
        if (mean_acc<self.params['dock']['min_repX_acc']) and \
            (self.params['dock']['protocol']=='Adaptive'):
          # If the acceptance probability is too low,
          # reject the state and restart
          self.dock_protocol.pop()
          confs = confs_o
          E = E_o
          rejectStage += 1
          self.tee("  rejected new state with low estimated acceptance rate")
        elif len(self.dock_protocol)>(2+(not undock)) and \
            (mean_acc>0.99) and (not lambda_n['crossed']) and \
            (self.params['dock']['protocol']=='Adaptive'):
          # If the acceptance probability is too high,
          # reject the previous state and restart
          self.confs['dock']['replicas'][-1] = confs[np.random.randint(len(confs))]
          self.dock_protocol.pop()
          self.dock_protocol[-1] = copy.deepcopy(lambda_n)
          rejectStage -= 1
          lambda_o = lambda_n
          self.tee("  rejected past state with high estimated acceptance rate")
        else:
          # Store data and continue with initialization
          self.confs['dock']['replicas'].append(confs[np.random.randint(len(confs))])
          self.confs['dock']['samples'].append([confs])
          self.dock_Es.append([E])
          self.dock_protocol[-1] = copy.deepcopy(lambda_n)
          rejectStage = 0
          lambda_o = lambda_n
      else:
        # Store data and continue with initialization (first time)
        self.confs['dock']['replicas'].append(confs[np.random.randint(len(confs))])
        self.confs['dock']['samples'].append([confs])
        self.dock_Es.append([E])
        self.dock_protocol[-1] = copy.deepcopy(lambda_n)
        rejectStage = 0
        lambda_o = lambda_n

      # Special tasks after the last stage
      if (self.dock_protocol[-1]['crossed']):
        # For undocking, reverse protocol and energies
        if undock:
          self.tee("  reversing replicas, samples, and protocol")
          self.confs['dock']['replicas'].reverse()
          self.confs['dock']['samples'].reverse()
          self.confs['dock']['seeds'] = None
          self.dock_Es.reverse()
          self.dock_protocol.reverse()
          self.dock_protocol[0]['crossed'] = False
          self.dock_protocol[-1]['crossed'] = True

        if (not self.params['dock']['keep_intermediate']):
          for k in range(1,len(self.dock_protocol)-1):
            self.confs['dock']['samples'][k] = []

        self._dock_cycle += 1

      # Save progress every 10 minutes
      if ((time.time()-self.start_times['dock_save'])>(10*60)):
        self._save('dock')
        self.start_times['dock_save'] = time.time()
        saved = True
      else:
        saved = False

      if self._run_type=='timed':
        remaining_time = self.timings['max']*60 - \
          (time.time()-self.start_times['run'])
        if remaining_time<0:
          if not saved:
            self._save('dock')
          self.tee("  no time remaining for initial dock")
          self._clear_lock('dock')
          return False

    if not saved:
      self._save('dock')

    self.tee("\nElapsed time for initial docking of " + \
      "%d states: "%len(self.dock_protocol) + \
      HMStime(time.time()-self.start_times['initial_dock']))
    self._clear_lock('dock')
    self.sampler['dock_SmartDarting'].confs = []
    return True

  def calc_f_RL(self, readOnly=False, do_solvation=True, redo=False):
    """
    Calculates the binding potential of mean force
    redo recalculates f_RL and B except grid_MBAR 
    """
    if self.dock_protocol==[]:
      return # Initial docking is incomplete

    # Initialize variables as empty lists or by loading data
    if self.params['dock']['pose']==-1:
      f_RL_FN = os.path.join(self.dir['dock'],'f_RL.pkl.gz')
    else:
      f_RL_FN = os.path.join(self.dir['dock'], \
        'f_RL_pose%03d.pkl.gz'%self.params['dock']['pose'])
    
    dat = self._load_pkl_gz(f_RL_FN)
    if (dat is not None):
      (self.f_L, self.stats_RL, self.f_RL, self.B) = dat
    else:
      self._clear_f_RL()
    if readOnly:
      return True

    if redo:
      for key in self.f_RL.keys():
        if key!='grid_MBAR':
          self.f_RL[key] = []
      self.B = {'MMTK_MBAR':[]}
      for phase in self.params['dock']['phases']:
        for method in ['min_Psi','mean_Psi','EXP','MBAR']:
          self.B[phase+'_'+method] = []

    # Make sure all the energies are available
    for c in range(self._dock_cycle):
      if len(self.dock_Es[-1][c].keys())==0:
        self.tee("  skipping the binding PMF calculation")
        return
    if not hasattr(self,'f_L'):
      self.tee("  skipping the binding PMF calculation")
      return

    start_string = "\n>>> Complex free energy calculations, starting at " + \
      time.strftime("%a, %d %b %Y %H:%M:%S", time.localtime()) + "\n"
    self.start_times['BPMF'] = time.time()

    updated = False
    def set_updated_to_True(updated, quiet=False):
      if (updated is False) and (not quiet):
        self.tee(start_string)
      updated = True
      return updated

    K = len(self.dock_protocol)
    
    # Store stats_RL
    # Internal energies
    self.stats_RL['u_K_sampled'] = \
      [self._u_kln([self.dock_Es[-1][c]],[self.dock_protocol[-1]]) \
        for c in range(self._dock_cycle)]
    self.stats_RL['u_KK'] = \
      [np.sum([self._u_kln([self.dock_Es[k][c]],[self.dock_protocol[k]]) \
        for k in range(len(self.dock_protocol))],0) \
          for c in range(self._dock_cycle)]

    # Interaction energies
    for c in range(len(self.stats_RL['Psi_grid']), self._dock_cycle):
      self.stats_RL['Psi_grid'].append(
          (self.dock_Es[-1][c]['LJr'] + \
           self.dock_Es[-1][c]['LJa'] + \
           self.dock_Es[-1][c]['ELE'])/self.RT_SIMMIN)
      updated = set_updated_to_True(updated, quiet=~do_solvation)

    # Estimate cycle at which simulation has equilibrated
    eqc_o = self.stats_RL['equilibrated_cycle']
    self.stats_RL['equilibrated_cycle'] = self._get_equilibrated_cycle('dock')
    if self.stats_RL['equilibrated_cycle']!=eqc_o:
      updated = set_updated_to_True(updated, quiet=~do_solvation)

    # Store rmsd values
    if redo and (self.params['dock']['rmsd'] is not False):
      k = len(self.dock_protocol) - 1
      for c in range(self._dock_cycle):
        confs = [conf[self.molecule.prmtop_atom_order,:]*10. \
          for conf in self.confs['dock']['samples'][k][c]]
        self.dock_Es[k][c]['rmsd'] = self.get_rmsds(confs)
    self.stats_RL['rmsd'] = [(np.hstack([self.dock_Es[k][c]['rmsd']
      if 'rmsd' in self.dock_Es[k][c].keys() else [] \
        for c in range(self.stats_RL['equilibrated_cycle'][-1], \
                       self._dock_cycle)])) \
          for k in range(len(self.dock_protocol))]

    # Calculate docking free energies that have not already been calculated
    while len(self.f_RL['grid_MBAR'])<self._dock_cycle:
      self.f_RL['grid_MBAR'].append([])
    while len(self.stats_RL['mean_acc'])<self._dock_cycle:
      self.stats_RL['mean_acc'].append([])
    
    for c in range(self._dock_cycle):
      # If solvation free energies are not being calculated,
      # only calculate the grid free energy for the current cycle
      if (not do_solvation) and c<(self._dock_cycle-1):
        continue
      if self.f_RL['grid_MBAR'][c]!=[]:
        continue

      fromCycle = self.stats_RL['equilibrated_cycle'][c]
      extractCycles = range(fromCycle, c+1)
      
      # Extract relevant energies
      dock_Es = [Es[fromCycle:c+1] \
        for Es in self.dock_Es]
      
      # Use MBAR for the grid scaling free energy estimate
      (u_kln,N_k) = self._u_kln(dock_Es,self.dock_protocol)
      MBAR = self._run_MBAR(u_kln,N_k)[0]
      self.f_RL['grid_MBAR'][c] = MBAR
      updated = set_updated_to_True(updated, quiet=~do_solvation)
      
      self.tee("  calculated grid scaling free energy of %.2f RT "%(\
                  self.f_RL['grid_MBAR'][c][-1])+\
               "using cycles %d to %d"%(fromCycle, c))

      # Average acceptance probabilities
      mean_acc = np.zeros(K-1)
      for k in range(0, K-1):
        (u_kln,N_k) = self._u_kln(dock_Es[k:k+2],self.dock_protocol[k:k+2])
        N = min(N_k)
        acc = np.exp(-u_kln[0,1,:N]-u_kln[1,0,:N]+u_kln[0,0,:N]+u_kln[1,1,:N])
        mean_acc[k] = np.mean(np.minimum(acc,np.ones(acc.shape)))
      self.stats_RL['mean_acc'][c] = mean_acc

    if not do_solvation:
      if updated:
        if not self._run_type=='timed':
          self._write_pkl_gz(f_RL_FN, \
            (self.f_L, self.stats_RL, self.f_RL, self.B))
        self._clear_lock('dock')
      return True

    # Make sure postprocessing is complete
    pp_complete = self._postprocess()
    if not pp_complete:
      return False
    self.calc_f_L()

    # Make sure all the phase energies are available
    for c in range(self._dock_cycle):
      for phase in self.params['dock']['phases']:
        for prefix in ['L','RL']:
          if not prefix+phase in self.dock_Es[-1][c].keys():
            self.tee("  postprocessed energies for %s unavailable"%phase)
            return

    # Store stats_RL internal energies for phases
    for phase in self.params['dock']['phases']:
      self.stats_RL['u_K_'+phase] = \
        [self.dock_Es[-1][c]['RL'+phase][:,-1]/self.RT_SIMMIN \
          for c in range(self._dock_cycle)]

    # Interaction energies
    for phase in self.params['dock']['phases']:
      if (not 'Psi_'+phase in self.stats_RL):
        self.stats_RL['Psi_'+phase] = []
      for c in range(len(self.stats_RL['Psi_'+phase]), self._dock_cycle):
        self.stats_RL['Psi_'+phase].append(
          (self.dock_Es[-1][c]['RL'+phase][:,-1] - \
           self.dock_Es[-1][c]['L'+phase][:,-1] - \
           self.original_Es[0][0]['R'+phase][:,-1])/self.RT_SIMMIN)

    # Predict native pose
    if self.params['dock']['pose']==-1:
      (self.stats_RL['pose_inds'], self.stats_RL['scores']) = \
        self._get_pose_prediction('dock', self.stats_RL['equilibrated_cycle'][-1])

    # BPMF assuming receptor and complex solvation cancel
    self.B['MMTK_MBAR'] = [-self.f_L['cool_MBAR'][-1][-1] + \
      self.f_RL['grid_MBAR'][c][-1] for c in range(len(self.f_RL['grid_MBAR']))]

    # BPMFs
    for phase in self.params['dock']['phases']:
      for key in [phase+'_solv']:
        if not key in self.f_RL:
          self.f_RL[key] = []
      for method in ['min_Psi','mean_Psi','EXP','MBAR']:
        if not phase+'_'+method in self.B:
          self.B[phase+'_'+method] = []

      # Receptor solvation
      f_R_solv = self.original_Es[0][0]['R'+phase][:,-1]/self.RT_TARGET

      for c in range(len(self.B[phase+'_MBAR']), self._dock_cycle):
        updated = set_updated_to_True(updated)
        extractCycles = range(self.stats_RL['equilibrated_cycle'][c], c+1)

        # From the full grid to the fully bound complex in phase
        u_RL = np.concatenate([\
          self.dock_Es[-1][c]['RL'+phase][:,-1]/self.RT_TARGET \
          for c in extractCycles])
        u_sampled = np.concatenate([\
          self.stats_RL['u_K_sampled'][c] for c in extractCycles])

        du = u_RL - u_sampled
        min_du = min(du)
        weights = np.exp(-du+min_du)

        # Filter outliers
        if self.params['dock']['pose']>-1:
          toKeep = du > (np.mean(du) - 3*np.std(du))
          du = du[toKeep]
          weights[~toKeep] = 0.

        weights = weights/sum(weights)

        # Exponential average
        f_RL_solv = -np.log(np.exp(-du+min_du).mean()) + min_du - f_R_solv

        # Interaction energies
        Psi = np.concatenate([self.stats_RL['Psi_'+phase][c] \
          for c in extractCycles])
        min_Psi = min(Psi)
        max_Psi = max(Psi)
    
        # Complex solvation
        self.f_RL[phase+'_solv'].append(f_RL_solv)
        
        # Various BPMF estimates
        self.B[phase+'_min_Psi'].append(min_Psi)
        self.B[phase+'_mean_Psi'].append(np.sum(weights*Psi))
        self.B[phase+'_EXP'].append(\
          np.log(sum(weights*np.exp(Psi-max_Psi))) + max_Psi)
        
        self.B[phase+'_MBAR'].append(\
          - self.f_L[phase+'_solv'][-1] - self.f_L['cool_MBAR'][-1][-1] \
          + self.f_RL['grid_MBAR'][-1][-1] + f_RL_solv)

        self.tee("  calculated %s binding PMF of %.5g RT with cycles %d to %d"%(\
          phase, self.B[phase+'_MBAR'][-1], \
          self.stats_RL['equilibrated_cycle'][c], c))

    if updated:
      self._write_pkl_gz(f_RL_FN, (self.f_L, self.stats_RL, self.f_RL, self.B))
      self.tee("\nElapsed time for binding PMF estimation: " + \
        HMStime(time.time()-self.start_times['BPMF']))
    self._clear_lock('dock')
    
  def _store_infinite_f_RL(self):
    if self.params['dock']['pose']==-1:
      f_RL_FN = os.path.join(self.dir['dock'],'f_RL.pkl.gz')
    else:
      f_RL_FN = os.path.join(self.dir['dock'],\
        'f_RL_pose%03d.pkl.gz'%self.params['dock']['pose'])
    self._write_pkl_gz(f_RL_FN, (self.f_L, [], np.inf, np.inf))

  def _get_equilibrated_cycle(self, process):
    process_Es = getattr(self,'%s_Es'%process)

    # Get previous results, if any
    if process=='cool':
      if hasattr(self,'stats_L') and \
          ('equilibrated_cycle' in self.stats_L.keys()) and \
          self.stats_L['equilibrated_cycle']!=[]:
        equilibrated_cycle = self.stats_L['equilibrated_cycle']
      else:
        equilibrated_cycle = [0]
    elif process=='dock':
      if hasattr(self,'stats_RL') and \
          ('equilibrated_cycle' in self.stats_RL.keys()) and \
          self.stats_RL['equilibrated_cycle']!=[]:
        equilibrated_cycle = self.stats_RL['equilibrated_cycle']
      else:
        equilibrated_cycle = [0]

    # Estimate equilibrated cycle
    for last_c in range(len(equilibrated_cycle), \
        getattr(self,'_%s_cycle'%process)):
      correlation_times = [np.inf] + [\
        pymbar.timeseries.integratedAutocorrelationTime(\
          np.concatenate([process_Es[0][c]['mean_energies'] \
            for c in range(start_c,len(process_Es[0])) \
            if 'mean_energies' in process_Es[0][c].keys()])) \
               for start_c in range(1,last_c)]
      g = 2*np.array(correlation_times) + 1
      nsamples_tot = [n for n in reversed(np.cumsum([len(process_Es[0][c]['MM']) \
        for c in reversed(range(last_c))]))]
      nsamples_ind = nsamples_tot/g
      equilibrated_cycle_last_c = max(np.argmax(nsamples_ind),1)
      equilibrated_cycle.append(equilibrated_cycle_last_c)
      
    return equilibrated_cycle

  def _get_pose_prediction(self, process, equilibrated_cycle):
    if process=='dock':
      stats = self.stats_RL
      compareToRef = self.params['dock']['rmsd']
    else:
      stats = self.stats_L
      compareToRef = False

    # Gather snapshots
    for k in range(equilibrated_cycle,getattr(self,'_%s_cycle'%process)):
      if not isinstance(self.confs[process]['samples'][-1][k], list):
        self.confs[process]['samples'][-1][k] = [self.confs[process]['samples'][-1][k]]
    import itertools
    confs = np.array([conf[self.molecule.heavy_atoms,:] \
      for conf in itertools.chain.from_iterable(\
      [self.confs[process]['samples'][-1][c] \
        for c in range(equilibrated_cycle,getattr(self,'_%s_cycle'%process))])])
    cum_Nk = np.cumsum([0] + [len(self.confs[process]['samples'][-1][c]) \
      for c in range(equilibrated_cycle,getattr(self,'_%s_cycle'%process))])

    # RMSD matrix
    from pyRMSD.matrixHandler import MatrixHandler
    rmsd_matrix_handler = MatrixHandler().createMatrix(confs, \
      {'cool':'QCP_SERIAL_CALCULATOR', \
       'dock':'NOSUP_SERIAL_CALCULATOR'}[process])
    rmsd_matrix = rmsd_matrix_handler.get_data()
    rmsd_matrix = np.clip(rmsd_matrix, 0., None)

    # Clustering
    import scipy.cluster
    Z = scipy.cluster.hierarchy.linkage(rmsd_matrix, method='complete')
    assignments = np.array(\
      scipy.cluster.hierarchy.fcluster(Z, 0.1, criterion='distance'))

    # Reindexes the assignments in order of appearance
    new_index = 0
    mapping_to_new_index = {}
    for assignment in assignments:
      if not assignment in mapping_to_new_index.keys():
        mapping_to_new_index[assignment] = new_index
        new_index += 1
    assignments = [mapping_to_new_index[a] for a in assignments]

    # Gets the medoid of every cluster and store rmsd if relevant
    def linear_index_to_pair(ind):
      cycle = list(ind<cum_Nk).index(True)-1
      n = ind-cum_Nk[cycle]
      return (cycle + equilibrated_cycle,n)

    from scipy.spatial.distance import squareform
    rmsd_matrix = squareform(rmsd_matrix)
    pose_inds = []
    scores = {}
    if compareToRef:
      scores['rmsd'] = []
    for n in range(max(assignments)+1):
      inds = [i for i in range(len(assignments)) if assignments[i]==n]
      rmsd_matrix_n = rmsd_matrix[inds][:,inds]
      (cycle,n) = linear_index_to_pair(inds[np.argmin(np.mean(rmsd_matrix_n,0))])
      pose_inds.append((cycle,n))
      if compareToRef:
        scores['rmsd'].append(self.dock_Es[-1][cycle]['rmsd'][n])
        
    # Score clusters based on total energy
    uo = np.concatenate([stats['u_K_sampled'][c] \
      for c in range(equilibrated_cycle,getattr(self,'_%s_cycle'%process))])
    for phase in (['grid']+self.params[process]['phases']):
      if phase!='grid':
        un = np.concatenate([stats['u_K_'+phase][c] \
          for c in range(equilibrated_cycle,getattr(self,'_%s_cycle'%process))])
        du = un-uo
        min_du = min(du)
        weights = np.exp(-du+min_du)
      else:
        un = uo
        weights = np.ones(len(assignments))
      cluster_counts = np.histogram(assignments, \
        bins=np.arange(len(set(assignments))+1)-0.5,
        weights=weights)[0]
      # by free energy
      cluster_fe = -self.RT_TARGET*np.log(cluster_counts)
      cluster_fe -= np.min(cluster_fe)
      scores[phase+'_fe_u'] = cluster_fe
      # by minimum and mean energy
      scores[phase+'_min_u'] = []
      scores[phase+'_mean_u'] = []
      for n in range(max(assignments)+1):
        un_n = [un[i] for i in range(len(assignments)) if assignments[i]==n]
        scores[phase+'_min_u'].append(np.min(un_n))
        scores[phase+'_mean_u'].append(np.mean(un_n))
    
    if process=='dock':
      # Score clusters based on interaction energy
      Psi_o = np.concatenate([stats['Psi_grid'][c] \
        for c in range(equilibrated_cycle,getattr(self,'_%s_cycle'%process))])
      for phase in (['grid']+self.params[process]['phases']):
        if phase!='grid':
          Psi_n = np.concatenate([stats['Psi_'+phase][c] \
            for c in range(equilibrated_cycle,getattr(self,'_%s_cycle'%process))])
          dPsi = Psi_n-Psi_o
          min_dPsi = min(dPsi)
          weights = np.exp(-dPsi+min_dPsi)
        else:
          Psi_n = Psi_o
          weights = np.ones(len(assignments))
        cluster_counts = np.histogram(assignments, \
          bins=np.arange(len(set(assignments))+1)-0.5,
          weights=weights)[0]
        # by free energy
        cluster_fe = -self.RT_TARGET*np.log(cluster_counts)
        cluster_fe -= np.min(cluster_fe)
        scores[phase+'_fe_Psi'] = cluster_fe
        # by minimum and mean energy
        scores[phase+'_min_Psi'] = []
        scores[phase+'_mean_Psi'] = []
        for n in range(max(assignments)+1):
          Psi_n_n = [Psi_n[i] for i in range(len(assignments)) if assignments[i]==n]
          scores[phase+'_min_Psi'].append(np.min(Psi_n_n))
          scores[phase+'_mean_Psi'].append(np.mean(Psi_n_n))     

    for key in scores.keys():
      scores[key] = np.array(scores[key])
    
    return (pose_inds, scores)
    
  def configuration_energies(self, minimize=False, max_confs=None):
    """
    Calculates the energy for configurations from self._FNs['score']
    """
    # Determine the name of the file
    prefix = 'xtal' if self._FNs['score']=='default' else \
      os.path.basename(self._FNs['score']).split('.')[0]
    if minimize:
      prefix = 'min_' + prefix
    energyFN = os.path.join(self.dir['dock'],prefix+'.pkl.gz')

    # Set the force field to fully interacting
    lambda_o = self._lambda(1.0, 'dock')
    self._set_universe_evaluator(lambda_o)

    # Load the configurations
    if os.path.isfile(energyFN):
      (confs, Es) = self._load_pkl_gz(energyFN)
    else:
      (confs, Es) = self._get_confs_to_rescore(site=False, \
        minimize=minimize, sort=False)

    self._set_lock('dock')
    self.tee("\n>>> Calculating energies for %d configurations, "%len(confs) + \
      "starting at " + \
      time.strftime("%a, %d %b %Y %H:%M:%S", time.localtime()) + "\n")
    self.start_times['configuration_energies'] = time.time()

    updated = False
    # Calculate MM and OBC energies
    if not 'MM' in Es.keys():
      Es = self._energyTerms(confs, Es)
      solvation_o = self.params['dock']['solvation']
      self.params['dock']['solvation'] = 'Fractional'
      if 'OBC' in self._forceFields.keys():
        del self._forceFields['OBC']
      self._evaluators = {}
      self._set_universe_evaluator(lambda_o)
      Es = self._energyTerms(confs, Es)
      Es['OBC_Fractional'] = Es['OBC']
      self.params['dock']['solvation'] = 'Full'
      if 'OBC' in self._forceFields.keys():
        del self._forceFields['OBC']
      self._evaluators = {}
      self._set_universe_evaluator(lambda_o)
      Es = self._energyTerms(confs, Es)
      self.params['dock']['solvation'] = solvation_o
      updated = True
  
    # Direct electrostatic energy
    FN = os.path.join(os.path.dirname(self._FNs['grids']['ELE']), 'direct_ele.nc')
    if not 'direct_ELE' in Es.keys() and os.path.isfile(FN):
      key = 'direct_ELE'
      Es[key] = np.zeros(len(confs))
      from AlGDock.ForceFields.Grid.Interpolation import InterpolationForceField
      FF = InterpolationForceField(FN, \
        scaling_property='scaling_factor_electrostatic')
      self.universe.setForceField(FF)
      for c in range(len(confs)):
        self.universe.setConfiguration(Configuration(self.universe,confs[c]))
        Es[key][c] = self.universe.energy()
      updated = True

    # Calculate symmetry-corrected RMSD
    if (not 'rmsd' in Es.keys()) and (self.params['dock']['rmsd'] is not False):
      confs_prmtop_order = [conf[self.molecule.prmtop_atom_order,:]*10. \
        for conf in confs]
      Es['rmsd'] = self.get_rmsds(confs_prmtop_order)
      updated = True

    if updated:
      self.tee("\nElapsed time for ligand MM, OBC, and grid energies was " + \
        HMStime(time.time() - self.start_times['configuration_energies']), \
        process='dock')
    self._clear_lock('dock')

    # Reduce the number of conformations
    if max_confs is not None:
      confs = confs[:max_confs]

    # Implicit solvent energies
    self._load_programs(self.params['dock']['phases'])

    self.confs['dock']['starting_poses'] = None
    self._postprocess([('original',0, 0,'R')])

    for phase in self.params['dock']['phases']:
      if not 'R'+phase in Es.keys():
        Es['R'+phase] = self.params['dock']['receptor_'+phase]

    toClear = []
    for phase in self.params['dock']['phases']:
      for moiety in ['L','RL']:
        if not moiety+phase in Es.keys():
          outputname = os.path.join(self.dir['dock'],'%s.%s%s'%(prefix,moiety,phase))
          if phase.startswith('NAMD'):
            traj_FN = os.path.join(self.dir['dock'],'%s.%s.dcd'%(prefix,moiety))
            self._write_traj(traj_FN, confs, moiety)
          elif phase.startswith('sander'):
            traj_FN = os.path.join(self.dir['dock'],'%s.%s.mdcrd'%(prefix,moiety))
            self._write_traj(traj_FN, confs, moiety)
          elif phase.startswith('gbnsr6'):
            traj_FN = os.path.join(self.dir['dock'], \
              '%s.%s%s'%(prefix,moiety,phase),'in.crd')
          elif phase.startswith('OpenMM'):
            traj_FN = None
          elif phase in ['APBS_PBSA']:
            traj_FN = os.path.join(self.dir['dock'],'%s.%s.pqr'%(prefix,moiety))
          else:
            raise Exception('Unknown phase!')
          if not traj_FN in toClear:
            toClear.append(traj_FN)
          for program in ['NAMD','sander','gbnsr6','OpenMM','APBS']:
            if phase.startswith(program):
              # TODO: Mechanism to do partial calculation
              Es[moiety+phase] = getattr(self,'_%s_Energy'%program)(confs, \
                moiety, phase, traj_FN, outputname, debug=DEBUG)
              updated = True
              # Get any data added since the calculation started
              if os.path.isfile(energyFN):
                (confs_o, Es_o) = self._load_pkl_gz(energyFN)
                for key in Es_o.keys():
                  if key not in Es.keys():
                    Es[key] = Es_o[key]
              # Store the data
              self._write_pkl_gz(energyFN,(confs,Es))
              break
    for FN in toClear:
      if os.path.isfile(FN):
        os.remove(FN)

    for key in Es.keys():
      Es[key] = np.array(Es[key])
    self._combine_MM_and_solvent(Es)

    if updated:
      self._set_lock('dock')
      self.tee("\nElapsed time for energies was " + \
        HMStime(time.time() - self.start_times['configuration_energies']), \
        process='dock')
      self._clear_lock('dock')

      # Get any data added since the calculation started
      if os.path.isfile(energyFN):
        (confs_o, Es_o) = self._load_pkl_gz(energyFN)
        for key in Es_o.keys():
          if key not in Es.keys():
            Es[key] = Es_o[key]

      # Store the data
      self._write_pkl_gz(energyFN,(confs,Es))
    return (confs,Es)

  ######################
  # Internal Functions #
  ######################

  def _set_universe_evaluator(self, lambda_n):
    """
    Sets the universe evaluator to values appropriate for the given lambda_n dictionary.
    The elements in the dictionary lambda_n can be:
      MM - True, to turn on the Generalized AMBER force field
      site - True, to turn on the binding site
      sLJr - scaling of the soft Lennard-Jones repulsive grid
      sLJa - scaling of the soft Lennard-Jones attractive grid
      sELE - scaling of the soft electrostatic grid
      LJr - scaling of the Lennard-Jones repulsive grid
      LJa - scaling of the Lennard-Jones attractive grid
      ELE - scaling of the electrostatic grid
      hwidth_angular_int - half width of flat-bottom wells for angular internal degrees of freedom (radians)
      k_angular_int - spring constant of flat-bottom wells for angular internal degrees of freedom (kJ/nm)
      hwidth_spatial_ext - half width of flat-bottom wells for spatial external degrees of freedom (nm)
      k_spatial_ext - spring constant of flat-bottom wells for spatial external degrees of freedom (kJ/nm)
      hwidth_angular_ext - half width of flat-bottom wells for angular external degrees of freedom (radians)
      k_angular_ext - spring constant of flat-bottom wells for angular external degrees of freedom (kJ/nm)
      T - the temperature in K
    """

    self.T = lambda_n['T']
    self.RT = R*lambda_n['T']
    
    # Reuse evaluators that have been stored
    evaluator_key = ','.join(['%s:%s'%(k,lambda_n[k]) \
      for k in sorted(lambda_n.keys())])
    if evaluator_key in self._evaluators.keys():
      self.universe._evaluator[(None,None,None)] = \
        self._evaluators[evaluator_key]
      return
    
    # Otherwise create a new evaluator
    fflist = []
    if ('MM' in lambda_n.keys()) and lambda_n['MM']:
      fflist.append(self._forceFields['gaff'])
    if ('site' in lambda_n.keys()) and lambda_n['site']:
      if not 'site' in self._forceFields.keys():
        # Set up the binding site in the force field
        if (self.params['dock']['site']=='Measure'):
          self.params['dock']['site'] = 'Sphere'
          if self.params['dock']['site_measured'] is not None:
            (self.params['dock']['site_max_R'],self.params['dock']['site_center']) = \
              self.params['dock']['site_measured']
          else:
            print '\n*** Measuring the binding site ***'
            self._set_universe_evaluator(self._lambda(1.0, 'dock', site=False))
            (confs, Es) = self._get_confs_to_rescore(site=False, minimize=True)
            if len(confs)>0:
              # Use the center of mass for configurations
              # within 20 RT of the lowest energy
              cutoffE = Es['total'][-1] + 20*self.RT_SIMMIN
              coms = []
              for (conf,E) in reversed(zip(confs,Es['total'])):
                if E<=cutoffE:
                  self.universe.setConfiguration(Configuration(self.universe,conf))
                  coms.append(np.array(self.universe.centerOfMass()))
                else:
                  break
              print '  %d configurations fit in the binding site'%len(coms)
              coms = np.array(coms)
              center = (np.min(coms,0)+np.max(coms,0))/2
              max_R = max(np.ceil(np.max(np.sqrt(np.sum((coms-center)**2,1)))*10.)/10.,0.6)
              self.params['dock']['site_max_R'] = max_R
              self.params['dock']['site_center'] = center
              self.universe.setConfiguration(Configuration(self.universe,confs[-1]))
            if ((self.params['dock']['site_max_R'] is None) or \
                (self.params['dock']['site_center'] is None)):
              raise Exception('No binding site parameters!')
            else:
              self.params['dock']['site_measured'] = \
                (self.params['dock']['site_max_R'], \
                 self.params['dock']['site_center'])

        if (self.params['dock']['site']=='Sphere') and \
           (self.params['dock']['site_center'] is not None) and \
           (self.params['dock']['site_max_R'] is not None):
          from AlGDock.ForceFields.Sphere.Sphere import SphereForceField
          self._forceFields['site'] = SphereForceField(
            center=self.params['dock']['site_center'],
            max_R=self.params['dock']['site_max_R'], name='site')
        elif (self.params['dock']['site']=='Cylinder') and \
             (self.params['dock']['site_center'] is not None) and \
             (self.params['dock']['site_direction'] is not None):
          from AlGDock.ForceFields.Cylinder.Cylinder import CylinderForceField
          self._forceFields['site'] = CylinderForceField(
            origin=self.params['dock']['site_center'],
            direction=self.params['dock']['site_direction'],
            max_Z=self.params['dock']['site_max_Z'],
            max_R=self.params['dock']['site_max_R'], name='site')
        else:
          raise Exception('Binding site type not recognized!')
      fflist.append(self._forceFields['site'])

    # Add scalable terms
    for scalable in self._scalables:
      if (scalable in lambda_n.keys()) and lambda_n[scalable]>0:
        # Load the force field if it has not been loaded
        if not scalable in self._forceFields.keys():
          if scalable=='OBC':
            from AlGDock.ForceFields.OBC.OBC import OBCForceField
            if self.params['dock']['solvation']=='Fractional' and \
                ('ELE' in lambda_n.keys()):
              self._forceFields['OBC'] = OBCForceField(\
                desolvationGridFN=self._FNs['grids']['desolv'])
              self.tee('  %s grid loaded from %s in %s'%(scalable, \
                os.path.basename(self._FNs['grids']['desolv']), \
                HMStime(time.time()-self.start_times['grid_loading'])))
            else:
              self._forceFields['OBC'] = OBCForceField()
          else: # Grids
            self.start_times['grid_loading'] = time.time()
            grid_FN = self._FNs['grids'][{'sLJr':'LJr','sLJa':'LJa','sELE':'ELE',
              'LJr':'LJr','LJa':'LJa','ELE':'ELE'}[scalable]]
            grid_scaling_factor = 'scaling_factor_' + \
              {'sLJr':'LJr','sLJa':'LJa','sELE':'electrostatic', \
               'LJr':'LJr','LJa':'LJa','ELE':'electrostatic'}[scalable]

            # Determine the grid threshold
            if scalable=='sLJr':
              grid_thresh = 10.0
            elif scalable=='sELE':
              # The maximum value is set so that the electrostatic energy
              # less than or equal to the Lennard-Jones repulsive energy
              # for every heavy atom at every grid point
              scaling_factors_ELE = np.array([ \
                self.molecule.getAtomProperty(a, 'scaling_factor_electrostatic') \
                  for a in self.molecule.atomList()],dtype=float)
              scaling_factors_LJr = np.array([ \
                self.molecule.getAtomProperty(a, 'scaling_factor_LJr') \
                  for a in self.molecule.atomList()],dtype=float)
              toKeep = np.logical_and(scaling_factors_LJr>10., abs(scaling_factors_ELE)>0.1)
              scaling_factors_ELE = scaling_factors_ELE[toKeep]
              scaling_factors_LJr = scaling_factors_LJr[toKeep]
              grid_thresh = min(abs(scaling_factors_LJr*10.0/scaling_factors_ELE))
            else:
              grid_thresh = -1 # There is no threshold for grid points

            from AlGDock.ForceFields.Grid.Interpolation \
              import InterpolationForceField
            self._forceFields[scalable] = InterpolationForceField(grid_FN, \
              name=scalable, interpolation_type='Trilinear', \
              strength=lambda_n[scalable], scaling_property=grid_scaling_factor,
              inv_power=4 if scalable=='LJr' else None, \
              grid_thresh=grid_thresh)
            self.tee('  %s grid loaded from %s in %s'%(scalable, \
              os.path.basename(grid_FN), \
              HMStime(time.time()-self.start_times['grid_loading'])))

        # Set the force field strength to the desired value
        self._forceFields[scalable].set_strength(lambda_n[scalable])
        fflist.append(self._forceFields[scalable])

    if ('k_angular_int' in lambda_n.keys()) or \
       ('k_spatial_ext' in lambda_n.keys()) or \
       ('k_angular_ext' in lambda_n.keys()):
       
      # Load the force field if it has not been loaded
      if not ('ExternalRestraint' in self._forceFields.keys()):
        # Obtain reference pose
        if ('starting_poses' in self.confs['dock'].keys()) and \
           (self.confs['dock']['starting_poses'] is not None):
          starting_pose = np.copy(self.confs['dock']['starting_poses'][0])
        else:
          (confs, Es) = self._get_confs_to_rescore(site=False, \
            minimize=False, sort=False)
          if self.params['dock']['pose']<len(confs):
            starting_pose = np.copy(confs[self.params['dock']['pose']])
            self.confs['dock']['starting_poses'] = [np.copy(starting_pose)]
          else:
            self._clear('dock')
            self._store_infinite_f_RL()
            raise Exception('Pose index greater than number of poses')

        Xo = np.copy(self.universe.configuration().array)
        self.universe.setConfiguration(Configuration(self.universe, starting_pose))
        import AlGDock.RigidBodies
        rb = AlGDock.RigidBodies.identifier(self.universe, self.molecule)
        (TorsionRestraintSpecs, ExternalRestraintSpecs) = rb.poseInp()
        self.universe.setConfiguration(Configuration(self.universe, Xo))

        # Create force fields
        from AlGDock.ForceFields.Pose.PoseFF import InternalRestraintForceField
        self._forceFields['InternalRestraint'] = \
          InternalRestraintForceField(TorsionRestraintSpecs)
        from AlGDock.ForceFields.Pose.PoseFF import ExternalRestraintForceField
        self._forceFields['ExternalRestraint'] = \
          ExternalRestraintForceField(*ExternalRestraintSpecs)

      # Set parameter values
      if ('hwidth_angular_int' in lambda_n.keys()):
        self._forceFields['InternalRestraint'].set_hwidth(\
          lambda_n['hwidth_angular_int'])
      if ('k_angular_int' in lambda_n.keys()):
        self._forceFields['InternalRestraint'].set_k(\
          lambda_n['k_angular_int'])
        fflist.append(self._forceFields['InternalRestraint'])

      if ('hwidth_spatial_exp' in lambda_n.keys()):
        self._forceFields['ExternalRestraint'].set_hwidth_spatial(\
          lambda_n['hwidth_spatial_exp'])
      if ('k_spatial_ext' in lambda_n.keys()):
        self._forceFields['ExternalRestraint'].set_k_spatial(\
          lambda_n['k_spatial_ext'])
        fflist.append(self._forceFields['ExternalRestraint'])

      if ('hwidth_angular_exp' in lambda_n.keys()):
        self._forceFields['ExternalRestraint'].set_hwidth_angular(\
          lambda_n['hwidth_angular_exp'])
      if ('k_angular_ext' in lambda_n.keys()):
        self._forceFields['ExternalRestraint'].set_k_angular(\
          lambda_n['k_angular_ext'])

    compoundFF = fflist[0]
    for ff in fflist[1:]:
      compoundFF += ff
    self.universe.setForceField(compoundFF)

    eval = ForceField.EnergyEvaluator(\
      self.universe, self.universe._forcefield, None, None, None, None)
    eval.key = evaluator_key
    self.universe._evaluator[(None,None,None)] = eval
    self._evaluators[evaluator_key] = eval

  def _clear_evaluators(self):
    """
    Deletes the stored evaluators and grids to save memory
    """
    self._evaluators = {}
    for scalable in self._scalables:
      if (scalable in self._forceFields.keys()):
        del self._forceFields[scalable]

  def _ramp_T(self, T_START, T_LOW = 20., normalize=False):
    self.start_times['T_ramp'] = time.time()
  
    # First minimize the energy
    from MMTK.Minimization import SteepestDescentMinimizer # @UnresolvedImport
    minimizer = SteepestDescentMinimizer(self.universe)

    original_stderr = sys.stderr
    sys.stderr = NullDevice() # Suppresses warnings for minimization
    
    x_o = np.copy(self.universe.configuration().array)
    e_o = self.universe.energy()
    for rep in range(5000):
      minimizer(steps = 10)
      x_n = np.copy(self.universe.configuration().array)
      e_n = self.universe.energy()
      diff = abs(e_o-e_n)
      if np.isnan(e_n) or diff<0.05 or diff>1000.:
        self.universe.setConfiguration(Configuration(self.universe, x_o))
        break
      else:
        x_o = x_n
        e_o = e_n
  
    sys.stderr = original_stderr
    self.tee("  minimized to %.3g kcal/mol over %d steps"%(e_o, 10*(rep+1)))

    # Then ramp the energy to the starting temperature
    from AlGDock.Integrators.HamiltonianMonteCarlo.HamiltonianMonteCarlo \
      import HamiltonianMonteCarloIntegrator
    sampler = HamiltonianMonteCarloIntegrator(self.universe)

    e_o = self.universe.energy()
    T_LOW = 20.
    T_SERIES = T_LOW*(T_START/T_LOW)**(np.arange(30)/29.)
    for T in T_SERIES:
      delta_t = 2.0*MMTK.Units.fs
      steps_per_trial = 10
      attempts_left = 10
      while attempts_left>0:
        random_seed = int(T*10000) + attempts_left + \
          int(self.universe.configuration().array[0][0]*10000)
        if self._random_seed==0:
          random_seed += int(time.time()*1000)
        random_seed = random_seed%32767
        (xs, energies, acc, ntrials, delta_t) = \
          sampler(steps = 2500, steps_per_trial = 10, T=T,\
                  delta_t=delta_t, random_seed=random_seed)
        attempts_left -= 1
        acc_rate = float(acc)/ntrials  
        if acc_rate<0.4:
          delta_t -= 0.25*MMTK.Units.fs
        else:
          attempts_left = 0
        if delta_t < 0.1*MMTK.Units.fs:
          delta_t = 0.1*MMTK.Units.fs
          steps_per_trial = max(int(steps_per_trial/2), 1)
      fmt = "  T = %d, delta_t = %.3f fs, steps_per_trial = %d, acc_rate = %.3f"
      self.tee(fmt%(T, delta_t*1000, steps_per_trial, acc_rate))
    if normalize:
      self.universe.normalizePosition()
    e_f = self.universe.energy()

    self.tee("  ramped temperature from %d to %d K in %s, "%(\
      T_LOW, T_START, HMStime(time.time()-self.start_times['T_ramp'])) + \
      "changing energy to %.3g kcal/mol"%(e_f))

  def _initial_sim_state(self, seeds, process, lambda_k):
    """
    Initializes a state, returning the configurations and potential energy.
    Attempts simulation up to 5 times, adjusting the time step.
    """
    
    if not 'delta_t' in lambda_k.keys():
      lambda_k['delta_t'] = 1.*self.params[process]['delta_t']*MMTK.Units.fs
    lambda_k['steps_per_trial'] = self.params[process]['steps_per_sweep']

    attempts_left = 12
    while (attempts_left>0):
      # Get initial potential energy
      Es_o = []
      for seed in seeds:
        self.universe.setConfiguration(Configuration(self.universe, seed))
        Es_o.append(self.universe.energy())
      Es_o = np.array(Es_o)
    
      # Perform simulation
      results = []
      if self._cores>1:
        # Multiprocessing code
        m = multiprocessing.Manager()
        task_queue = m.Queue()
        done_queue = m.Queue()
        for k in range(len(seeds)):
          task_queue.put((seeds[k], process, lambda_k, True, k))
        processes = [multiprocessing.Process(target=self._sim_one_state_worker, \
            args=(task_queue, done_queue)) for p in range(self._cores)]
        for p in range(self._cores):
          task_queue.put('STOP')
        for p in processes:
          p.start()
        for p in processes:
          p.join()
        results = [done_queue.get() for seed in seeds]
        for p in processes:
          p.terminate()
      else:
        # Single process code
        results = [self._sim_one_state(\
          seeds[k], process, lambda_k, True, k) for k in range(len(seeds))]

      seeds = [result['confs'] for result in results]
      Es_n = np.array([result['Etot'] for result in results])
      deltaEs = Es_n-Es_o
      attempts_left -= 1

      # Get the time step
      delta_t = np.array([result['delta_t'] for result in results])
      if np.std(delta_t)>1E-3:
        # If the integrator adapts the time step, take an average
        delta_t = min(max(np.mean(delta_t), \
          self.params[process]['delta_t']/5.0*MMTK.Units.fs), \
          self.params[process]['delta_t']*0.1*MMTK.Units.fs)
      else:
        delta_t = delta_t[0]

      # Adjust the time step
      if 'HamiltonianMonteCarlo' in self.sampler[process].__module__:
        # Adjust the time step for Hamiltonian Monte Carlo
        acc_rate = float(np.sum([r['acc_Sampler'] for r in results]))/\
          np.sum([r['att_Sampler'] for r in results])
        if acc_rate>0.8:
          delta_t += 0.125*MMTK.Units.fs
        elif acc_rate<0.4:
          if delta_t<2.0*MMTK.Units.fs:
            lambda_k['steps_per_trial'] = max(int(lambda_k['steps_per_trial']/2.),1)
          delta_t -= 0.25*MMTK.Units.fs
          if acc_rate<0.1:
            delta_t -= 0.25*MMTK.Units.fs
        else:
          attempts_left = 0
      else:
        # For other integrators, make sure the time step
        # is small enough to see changes in the energy
        if (np.std(deltaEs)<1E-3):
          delta_t -= 0.25*MMTK.Units.fs
        else:
          attempts_left = 0
        
      if delta_t<0.1*MMTK.Units.fs:
        delta_t = 0.1*MMTK.Units.fs

      lambda_k['delta_t'] = delta_t

    sampler_metrics = ''
    for s in ['ExternalMC', 'SmartDarting', 'Sampler']:
      if np.array(['acc_'+s in r.keys() for r in results]).any():
        acc = np.sum([r['acc_'+s] for r in results])
        att = np.sum([r['att_'+s] for r in results])
        time = np.sum([r['time_'+s] for r in results])
        if att>0:
          sampler_metrics += '%s %d/%d=%.2f (%.1f s); '%(\
            s,acc,att,float(acc)/att,time)
    return (seeds, Es_n-Es_o, delta_t, sampler_metrics)
  
  def _replica_exchange(self, process):
    """
    Performs a cycle of replica exchange
    """
    if not process in ['dock','cool']:
      raise Exception('Process must be dock or cool')
# GMC
    def gMC_initial_setup():
      """
      Initialize BAT converter object.
      Decide which internal coord to crossover. Here, only the soft torsions will be crossovered.
      Produce a list of replica (state) index pairs to be swaped. Only Neighbor pairs will be swaped.
      Assume that self.universe, self.molecule and K (number of states) exist
      as global variables when the function is called.
      """
      from AlGDock.RigidBodies import identifier
      import itertools
      BAT_converter = identifier( self.universe, self.molecule )
      BAT = BAT_converter.BAT( extended = True )
      # this assumes that the torsional angles are stored in the tail of BAT
      softTorsionId = [ i + len(BAT) - BAT_converter.ntorsions for i in BAT_converter._softTorsionInd ]
      torsions_to_crossover = []
      for i in range(1, len(softTorsionId) ):
        combinations = itertools.combinations( softTorsionId, i )
        for c in combinations:
          torsions_to_crossover.append( list(c) )
      #
      BAT_converter.BAT_to_crossover = torsions_to_crossover
      if len( BAT_converter.BAT_to_crossover ) == 0:
        self.tee('  GMC No BAT to crossover')
      state_indices = range( K )
      state_indices_to_swap = zip( state_indices[0::2], state_indices[1::2] ) + \
                      zip( state_indices[1::2], state_indices[2::2] )
      #
      return BAT_converter, state_indices_to_swap
    #
    def do_gMC( nr_attempts, BAT_converter, state_indices_to_swap, torsion_threshold ):
      """
      Assume self.universe, confs, lambdas, state_inds, inv_state_inds exist as global variables
      when the function is called.
      If at least one of the torsions in the combination chosen for an crossover attempt
      changes more than torsion_threshold, the crossover will be attempted.
      The function will update confs.
      It returns the number of attempts and the number of accepted moves.
      """
      if nr_attempts < 0:
        raise Exception('Number of attempts must be nonnegative!')
      if torsion_threshold < 0.:
        raise Exception('Torsion threshold must be nonnegative!')
      #
      if len( BAT_converter.BAT_to_crossover ) == 0:
        return 0., 0.
      #
      from random import randrange
      # get reduced energies and BAT for all configurations in confs
      BATs = []
      energies = np.zeros( K, dtype = float )
      for c_ind in range(K):
        s_ind = state_inds[ c_ind ]
        self.universe.setConfiguration( Configuration( self.universe, confs[c_ind] ) )
        BATs.append( np.array( BAT_converter.BAT( extended = True ) , dtype = float ) )
        self._set_universe_evaluator( lambdas[ s_ind ] )
        reduced_e = self.universe.energy() / ( R*lambdas[ s_ind ]['T'] )
        energies[ c_ind ] = reduced_e
      #
      nr_sets_of_torsions = len( BAT_converter.BAT_to_crossover )
      #
      attempt_count , acc_count = 0 , 0
      sweep_count = 0
      while True:
        sweep_count += 1
        if (sweep_count * K) > (1000 * nr_attempts):
          self.tee('  GMC Sweep too many times, but few attempted. Consider reducing torsion_threshold.')
          return attempt_count, acc_count
        #
        for state_pair in state_indices_to_swap:
          conf_ind_k0 = inv_state_inds[ state_pair[0] ]
          conf_ind_k1 = inv_state_inds[ state_pair[1] ]
          # check if it should attempt for this pair of states
          ran_set_torsions = BAT_converter.BAT_to_crossover[ randrange( nr_sets_of_torsions ) ]
          do_crossover = np.any(np.abs(BATs[conf_ind_k0][ran_set_torsions] - BATs[conf_ind_k1][ran_set_torsions]) >= torsion_threshold)
          if do_crossover:
            attempt_count += 1
            # BAT and reduced energies before crossover
            BAT_k0_be = copy.deepcopy( BATs[conf_ind_k0] )
            BAT_k1_be = copy.deepcopy( BATs[conf_ind_k1] )
            e_k0_be = energies[conf_ind_k0]
            e_k1_be = energies[conf_ind_k1]
            # BAT after crossover
            BAT_k0_af = copy.deepcopy( BAT_k0_be )
            BAT_k1_af = copy.deepcopy( BAT_k1_be )
            for index in ran_set_torsions:
              tmp = BAT_k0_af[ index ]
              BAT_k0_af[ index ] = BAT_k1_af[ index ]
              BAT_k1_af[ index ] = tmp
            # Cartesian coord and reduced energies after crossover.
            BAT_converter.Cartesian( BAT_k0_af )
            self._set_universe_evaluator( lambdas[ state_pair[0] ] )
            e_k0_af = self.universe.energy() / ( R*lambdas[ state_pair[0] ]['T'] )
            conf_k0_af = copy.deepcopy( self.universe.configuration().array )
            #
            BAT_converter.Cartesian( BAT_k1_af )
            self._set_universe_evaluator( lambdas[ state_pair[1] ] )
            e_k1_af = self.universe.energy() / ( R*lambdas[ state_pair[1] ]['T'] )
            conf_k1_af = copy.deepcopy( self.universe.configuration().array )
            #
            de = ( e_k0_be - e_k0_af ) + ( e_k1_be - e_k1_af )
            # update confs, energies, BATS
            if (de > 0) or ( np.random.uniform() < np.exp(de) ):
              acc_count += 1
              confs[conf_ind_k0] = conf_k0_af
              confs[conf_ind_k1] = conf_k1_af
              #
              energies[conf_ind_k0] = e_k0_af
              energies[conf_ind_k1] = e_k1_af
              #
              BATs[conf_ind_k0] = BAT_k0_af
              BATs[conf_ind_k1] = BAT_k1_af
            #
            if attempt_count == nr_attempts:
              return attempt_count, acc_count
    #
    self._set_lock(process)

    cycle = getattr(self,'_%s_cycle'%process)
    confs = self.confs[process]['replicas']
    lambdas = getattr(self,process+'_protocol')

    terms = ['MM']
    if process=='cool':
      terms += ['OBC']
    elif process=='dock':
      if self.params['dock']['pose'] > -1:
        # Pose BPMF
        terms += ['k_angular_ext','k_spatial_ext','k_angular_int']
      else:
        terms += ['site']
      terms += self._scalables

    # A list of pairs of replica indicies
    K = len(lambdas)
    pairs_to_swap = []
    for interval in range(1,min(5,K)):
      lower_inds = []
      for lowest_index in range(interval):
        lower_inds += range(lowest_index,K-interval,interval)
      upper_inds = np.array(lower_inds) + interval
      pairs_to_swap += zip(lower_inds,upper_inds)

    from repX import attempt_swaps

    # Setting the force field will load grids
    # before multiple processes are spawned
    for k in range(K):
      self._set_universe_evaluator(lambdas[k])
    
    # If it has not been set up, set up Smart Darting
    if self.params[process]['darts_per_sweep']>0:
      if self.sampler[process+'_SmartDarting'].confs==[]:
        self.tee(self.sampler[process+'_SmartDarting'].set_confs(\
          self.confs[process]['SmartDarting']))
        self.confs[process]['SmartDarting'] = \
          self.sampler[process+'_SmartDarting'].confs
  
    # storage[key][sweep_index][state_index] will contain data
    # from the replica exchange sweeps
    storage = {}
    for var in ['confs','state_inds','energies']:
      storage[var] = []
    
    self.start_times['repX cycle'] = time.time()

    if self._cores>1:
      # Multiprocessing setup
      m = multiprocessing.Manager()
      task_queue = m.Queue()
      done_queue = m.Queue()

    # GMC
    do_gMC = self.params[process]['GMC_attempts'] > 0
    if do_gMC:
      self.tee('  Using GMC for %s' %process)
      nr_gMC_attempts = K * self.params[process]['GMC_attempts']
      torsion_threshold = self.params[process]['GMC_tors_threshold']
      gMC_attempt_count = 0
      gMC_acc_count     = 0
      time_gMC = 0.0
      BAT_converter, state_indices_to_swap = gMC_initial_setup()

    # MC move statistics
    acc = {}
    att = {}
    for move_type in ['ExternalMC','SmartDarting','Sampler']:
      acc[move_type] = np.zeros(K, dtype=int)
      att[move_type] = np.zeros(K, dtype=int)
      self.timings[move_type] = 0.
    self.timings['repX'] = 0.
    
    mean_energies = []

    # Do replica exchange
    state_inds = range(K)
    inv_state_inds = range(K)
    nsweeps = self.params[process]['sweeps_per_cycle']
    nsnaps = nsweeps/self.params[process]['snaps_per_cycle']
    for sweep in range(nsweeps):
      E = {}
      for term in terms:
        E[term] = np.zeros(K, dtype=float)
      # Sample within each state
      if self._cores>1:
        for k in range(K):
          task_queue.put((confs[k], process, lambdas[state_inds[k]], False, k))
        for p in range(self._cores):
          task_queue.put('STOP')
        processes = [multiprocessing.Process(target=self._sim_one_state_worker, \
            args=(task_queue, done_queue)) for p in range(self._cores)]
        for p in processes:
          p.start()
        for p in processes:
          p.join()
        unordered_results = [done_queue.get() for k in range(K)]
        results = sorted(unordered_results, key=lambda d: d['reference'])
        for p in processes:
          p.terminate()
      else:
        # Single process code
        results = [self._sim_one_state(confs[k], process, \
            lambdas[state_inds[k]], False, k) for k in range(K)]

      # GMC
      if do_gMC:
        time_start_gMC = time.time()
        att_count, acc_count = do_gMC( nr_gMC_attempts, BAT_converter, state_indices_to_swap, torsion_threshold )
        gMC_attempt_count += att_count
        gMC_acc_count     += acc_count
        time_gMC =+ ( time.time() - time_start_gMC )

      # Store energies
      for k in range(K):
        confs[k] = results[k]['confs']
      mean_energies.append(np.mean([results[k]['Etot'] for k in range(K)]))
      E = self._energyTerms(confs, E, process=process)

      # Store MC move statistics
      for k in range(K):
        for move_type in ['ExternalMC','SmartDarting','Sampler']:
          key = 'acc_'+move_type
          if key in results[k].keys():
            acc[move_type][state_inds[k]] += results[k][key]
            att[move_type][state_inds[k]] += results[k]['att_'+move_type]
            self.timings[move_type] += results[k]['time_'+move_type]

      # Calculate u_ij (i is the replica, and j is the configuration),
      #    a list of arrays
      (u_ij,N_k) = self._u_kln(E, [lambdas[state_inds[c]] for c in range(K)])
      # Do the replica exchange
      repX_start_time = time.time()
      (state_inds, inv_state_inds) = \
        attempt_swaps(state_inds, inv_state_inds, u_ij, pairs_to_swap, \
          self.params[process]['attempts_per_sweep'])
      self.timings['repX'] += (time.time()-repX_start_time)

      # Store data in local variables
      if (sweep+1)%self.params[process]['snaps_per_cycle']==0:
        if (process=='dock') and (self.params['dock']['rmsd'] is not False):
          confs_prmtop_order = [conf[self.molecule.prmtop_atom_order,:]*10. \
            for conf in confs]
          E['rmsd'] = self.get_rmsds(confs_prmtop_order)
        storage['confs'].append(list(confs))
        storage['state_inds'].append(list(state_inds))
        storage['energies'].append(copy.deepcopy(E))

    # GMC
    if do_gMC:
      self.tee('  {0}/{1} crossover attempts ({2:.3g}) accepted in {3}'.format(\
        gMC_acc_count, gMC_attempt_count, \
        float(gMC_acc_count)/float(gMC_attempt_count) \
          if gMC_attempt_count > 0 else 0, \
        HMStime(time_gMC)))

    # Report
    self.tee("  completed cycle %d in %s"%(cycle, \
      HMStime(time.time()-self.start_times['repX cycle'])))
    MC_report = " "
    for move_type in ['ExternalMC','SmartDarting','Sampler']:
      total_acc = np.sum(acc[move_type])
      total_att = np.sum(att[move_type])
      if total_att>0:
        MC_report += " %s %d/%d=%.2f (%.1f s);"%(move_type, \
          total_acc, total_att, float(total_acc)/total_att, \
          self.timings[move_type])
    MC_report += " repX t %.1f s"%self.timings['repX']
    self.tee(MC_report)

    # Adapt HamiltonianMonteCarlo parameters
    if 'HamiltonianMonteCarlo' in self.sampler[process].__module__:
      acc_rates = np.array(acc['Sampler'],dtype=np.float)/att['Sampler']
      for k in range(K):
        acc_rate = acc_rates[k]
        if acc_rate>0.8:
          lambdas[k]['delta_t'] += 0.125*MMTK.Units.fs
          lambdas[k]['steps_per_trial'] = min(lambdas[k]['steps_per_trial']*2,\
            self.params[process]['steps_per_sweep'])
        elif acc_rate<0.4:
          if lambdas[k]['delta_t']<2.0*MMTK.Units.fs:
            lambdas[k]['steps_per_trial'] = max(int(lambdas[k]['steps_per_trial']/2.),1)
          lambdas[k]['delta_t'] -= 0.25*MMTK.Units.fs
          if acc_rate<0.1:
            lambdas[k]['delta_t'] -= 0.25*MMTK.Units.fs
        if lambdas[k]['delta_t']<0.1*MMTK.Units.fs:
          lambdas[k]['delta_t'] = 0.1*MMTK.Units.fs

    # Get indicies for sorting by thermodynamic state, not replica
    inv_state_inds = np.zeros((nsnaps,K),dtype=int)
    for snap in range(nsnaps):
      state_inds = storage['state_inds'][snap]
      for k in range(K):
        inv_state_inds[snap][state_inds[k]] = k

    # Sort energies and conformations by thermodynamic state 
    # and store in global variables 
    #   self.process_Es and self.confs[process]['samples']
    # and also local variables 
    #   Es_repX and confs_repX
    if (process=='dock') and (self.params['dock']['rmsd'] is not False):
      terms.append('rmsd') # Make sure to save the rmsd
    Es_repX = []
    for k in range(K):
      E_k = {}
      E_k_repX = {}
      if k==0:
        E_k['acc'] = acc
        E_k['att'] = att
        E_k['mean_energies'] = mean_energies
      for term in terms:
        E_term = np.array([storage['energies'][snap][term][\
          inv_state_inds[snap][k]] for snap in range(nsnaps)])
        E_k[term] = E_term
        E_k_repX[term] = E_term
      getattr(self,process+'_Es')[k].append(E_k)
      Es_repX.append([E_k_repX])

    confs_repX = []
    for k in range(K):
      confs_k = [storage['confs'][snap][inv_state_inds[snap][k]] \
        for snap in range(nsnaps)]
      if self.params[process]['keep_intermediate'] or \
          ((process=='cool') and (k==0)) or (k==(K-1)):
        self.confs[process]['samples'][k].append(confs_k)
      confs_repX.append(confs_k)

    # Store final conformation of each replica
    self.confs[process]['replicas'] = \
      [np.copy(storage['confs'][-1][inv_state_inds[-1][k]]) \
       for k in range(K)]
        
    if self.params[process]['darts_per_sweep']>0:
      self._set_universe_evaluator(getattr(self,process+'_protocol')[-1])
      confs_SmartDarting = [np.copy(conf) \
        for conf in self.confs[process]['samples'][k][-1]]
      self.tee(self.sampler[process+'_SmartDarting'].set_confs(\
        confs_SmartDarting + self.confs[process]['SmartDarting']))
      self.confs[process]['SmartDarting'] = \
        self.sampler[process+'_SmartDarting'].confs

    setattr(self,'_%s_cycle'%process,cycle + 1)
    self._save(process)
    self.tee("")
    self._clear_lock(process)

    # The code below is only for sampling importance resampling
    if not self.params[process]['sampling_importance_resampling']:
      return

    # Calculate appropriate free energy
    if process=='cool':
      self.calc_f_L(do_solvation=False)
      f_k = self.f_L['cool_MBAR'][-1]
    elif process=='dock':
      self.calc_f_RL(do_solvation=False)
      f_k = self.f_RL['grid_MBAR'][-1]

    # Get weights for sampling importance resampling
    # MBAR weights for replica exchange configurations
    (u_kln,N_k) = self._u_kln(Es_repX,lambdas)

    # This is a more direct way to get the weights
    from pymbar.utils import kln_to_kn
    u_kn = kln_to_kn(u_kln, N_k=N_k)

    from pymbar.utils import logsumexp
    log_denominator_n = logsumexp(f_k - u_kn.T, b=N_k, axis=1)
    logW = f_k - u_kn.T - log_denominator_n[:, np.newaxis]
    W_nl = np.exp(logW)
    for k in range(K):
      W_nl[:,k] = W_nl[:,k]/np.sum(W_nl[:,k])

    # This is for conversion to 2 indicies: state and snapshot
    cum_N_state = np.cumsum([0] + list(N_k))

    def linear_index_to_snapshot_index(ind):
      state_index = list(ind<cum_N_state).index(True)-1
      nis_index = ind-cum_N_state[state_index]
      return (state_index,nis_index)

    # Selects new replica exchange snapshots
    self.confs[process]['replicas'] = []
    for k in range(K):
      (s,n) = linear_index_to_snapshot_index(\
        np.random.choice(range(W_nl.shape[0]), size = 1, p = W_nl[:,k])[0])
      self.confs[process]['replicas'].append(np.copy(confs_repX[s][n]))

  def _sim_one_state_worker(self, input, output):
    """
    Executes a task from the queue
    """
    for args in iter(input.get, 'STOP'):
      result = self._sim_one_state(*args)
      output.put(result)

  def _sim_one_state(self, seed, process, lambda_k, \
      initialize=False, reference=0):
    
    self.universe.setConfiguration(Configuration(self.universe, seed))
    
    self._set_universe_evaluator(lambda_k)
    if 'delta_t' in lambda_k.keys():
      delta_t = lambda_k['delta_t']
    else:
      raise Exception('No time step specified')
    if 'steps_per_trial' in lambda_k.keys():
      steps_per_trial = lambda_k['steps_per_trial']
    else:
      steps_per_trial = self.params[process]['steps_per_sweep']

    if initialize:
      steps = self.params[process]['steps_per_seed']
      ndarts = self.params[process]['darts_per_seed']
    else:
      steps = self.params[process]['steps_per_sweep']
      ndarts = self.params[process]['darts_per_sweep']
    
    random_seed = reference*reference + int(abs(seed[0][0]*10000))
    if self._random_seed>0:
      random_seed += self._random_seed
    else:
      random_seed += int(time.time()*1000)
    
    results = {}
    
    # Execute external MCMC moves
    if (process == 'dock') and (self.params['dock']['MCMC_moves']>0) \
        and (lambda_k['a'] < 0.1) and (self.params['dock']['pose']==-1):
      time_start_ExternalMC = time.time()
      dat = self.sampler['ExternalMC'](ntrials=5, T=lambda_k['T'])
      results['acc_ExternalMC'] = dat[2]
      results['att_ExternalMC'] = dat[3]
      results['time_ExternalMC'] = (time.time() - time_start_ExternalMC)

    # Execute dynamics sampler
    time_start_Sampler = time.time()
    dat = self.sampler[process](\
      steps=steps, steps_per_trial=steps_per_trial, \
      T=lambda_k['T'], delta_t=delta_t, \
      normalize=(process=='cool'), adapt=initialize, random_seed=random_seed)
    results['acc_Sampler'] = dat[2]
    results['att_Sampler'] = dat[3]
    results['delta_t'] = dat[4]
    results['time_Sampler'] = (time.time() - time_start_Sampler)

    # Execute smart darting
    if (ndarts>0) and not ((process == 'dock') and (lambda_k['a']<0.1)):
      time_start_SmartDarting = time.time()
      dat = self.sampler[process+'_SmartDarting'](\
        ntrials=ndarts, T=lambda_k['T'], random_seed=random_seed+5)
      results['acc_SmartDarting'] = dat[2]
      results['att_SmartDarting'] = dat[3]
      results['time_SmartDarting'] = (time.time() - time_start_SmartDarting)

    # Store and return results
    results['confs'] = np.copy(dat[0][-1])
    results['Etot'] = dat[1][-1]
    results['reference'] = reference

    return results

  def sim_process(self, process):
    """
    Simulate and analyze a cooling or docking process.
    
    As necessary, first conduct an initial cooling or docking
    and then run a desired number of replica exchange cycles.
    """
    if (getattr(self,process+'_protocol')==[]) or \
       (not getattr(self,process+'_protocol')[-1]['crossed']):
      time_left = getattr(self,'initial_'+process)()
      if not time_left:
        return False

    # Main loop for replica exchange
    if (self.params[process]['repX_cycles'] is not None) and \
       ((getattr(self,'_%s_cycle'%process) < \
         self.params[process]['repX_cycles'])):

      # Load configurations to score from another program
      if (process=='dock') and (self._dock_cycle==1) and \
         (self.params['dock']['pose'] == -1) and \
         (self._FNs['score'] is not None) and \
         (self._FNs['score']!='default'):
        self._set_lock('dock')
        self.tee("\n>>> Reinitializing replica exchange configurations")
        self._set_universe_evaluator(self._lambda(1.0, 'dock'))
        confs = self._get_confs_to_rescore(\
          nconfs=len(self.dock_protocol), site=True, minimize=True)[0]
        self._clear_lock('dock')
        if len(confs)>0:
          self.confs['dock']['replicas'] = confs

      self.tee("\n>>> Replica exchange for {0}ing, starting at {1}\n".format(\
        process, time.strftime("%a, %d %b %Y %H:%M:%S", time.localtime())), \
        process=process)
      self.start_times[process+'_repX_start'] = time.time()
      start_cycle = getattr(self,'_%s_cycle'%process)
      cycle_times = []
      while ((getattr(self,'_%s_cycle'%process) < self.params[process]['repX_cycles'])):
        self._replica_exchange(process)
        cycle_times.append(time.time()-self.start_times['repX cycle'])
        if process=='dock':
          self._insert_dock_state_between_low_acc()
        if self._run_type=='timed':
          remaining_time = self.timings['max']*60 - (time.time()-self.start_times['run'])
          cycle_time = np.mean(cycle_times)
          self.tee("  projected cycle time: %s, remaining time: %s"%(\
            HMStime(cycle_time), HMStime(remaining_time)), process=process)
          if cycle_time>remaining_time:
            return False
      self.tee("\nElapsed time for %d cycles of replica exchange was %s"%(\
         (getattr(self,'_%s_cycle'%process) - start_cycle), \
          HMStime(time.time() - self.start_times[process+'_repX_start'])), \
          process=process)

    # If there are insufficient configurations,
    #   do additional replica exchange on the cooling process
    if (process=='cool'):
      E_MM = []
      for k in range(len(self.cool_Es[0])):
        E_MM += list(self.cool_Es[0][k]['MM'])
      while len(E_MM)<self.params['dock']['seeds_per_state']:
        self.tee("More samples from high temperature ligand simulation needed", process='cool')
        self._replica_exchange('cool')
        cycle_times.append(time.time()-self.start_times['repX cycle'])
        if self._run_type=='timed':
          remaining_time = self.timings['max']*60 - (time.time()-self.start_times['run'])
          cycle_time = np.mean(cycle_times)
          self.tee("  projected cycle time: %s, remaining time: %s"%(\
            HMStime(cycle_time), HMStime(remaining_time)), process=process)
          if cycle_time>remaining_time:
            return False
        E_MM = []
        for k in range(len(self.cool_Es[0])):
          E_MM += list(self.cool_Es[0][k]['MM'])
    
    # Clear evaluators to save memory
    self._clear_evaluators()

    return True # The process has completed

  def _mixed_sampler2(self, steps, steps_per_trial, T, delta_t, random_seed, normalize=False, adapt=False): # EU
    ntrials = int(steps/steps_per_trial)

    (mixed_confs0, mixed_potEs0, mixed_accs0, mixed_ntrials0, mixed_dts0) = \
      self.mixed_samplers[0].Call(ntrials*5, 5, T, 0.0030, random_seed%254, 1, 1, 0.5)

    (mixed_confs1, mixed_potEs1, mixed_accs1, mixed_ntrials1, mixed_dts1) = \
      self.mixed_samplers[1](steps=steps, steps_per_trial=steps_per_trial, T=T, delta_t=delta_t, \
      normalize=normalize, adapt=adapt, random_seed=random_seed)

    mixed_confs = mixed_confs0 + mixed_confs1
    mixed_potEs = mixed_potEs0 + mixed_potEs1
    mixed_accs = mixed_accs0 + mixed_accs1
    mixed_ntrials = mixed_ntrials0 + mixed_ntrials1

    return (mixed_confs, mixed_potEs, mixed_accs, mixed_ntrials, delta_t)

  def _insert_dock_state(self, a, clear=True):
    """
    Inserts a new thermodynamic state into the docking protocol.
    Samples for previous cycles are added by sampling importance resampling.
    Clears grid_MBAR.
    """
    # Defines a new thermodynamic state based on the neighboring state
    neighbor_ind = [a<p['a'] for p in self.dock_protocol].index(True) - 1
    lambda_n = self._lambda(a, lambda_o=self.dock_protocol[neighbor_ind])

    # For sampling importance resampling,
    # prepare an augmented matrix for pymbar calculations
    # with a new thermodynamic state
    (u_kln_s,N_k) = self._u_kln(self.dock_Es,self.dock_protocol)
    (K,L,N) = u_kln_s.shape

    u_kln_n = self._u_kln(self.dock_Es,[lambda_n])[0]
    L += 1
    N_k = np.append(N_k,[0])

    u_kln = np.zeros([K,L,N])
    u_kln[:,:-1,:] = u_kln_s
    for k in range(K):
      u_kln[k,-1,:] = u_kln_n[k,0,:]

    # Determine SIR weights
    weights = self._run_MBAR(u_kln, N_k, augmented=True)[1][:,-1]
    weights = weights/sum(weights)
    
    # Resampling
    # Convert linear indices to 3 indicies: state, cycle, and snapshot
    cum_N_state = np.cumsum([0] + list(N_k))
    cum_N_cycle = [np.cumsum([0] + [self.dock_Es[k][c]['MM'].shape[0] \
      for c in range(len(self.dock_Es[k]))]) for k in range(len(self.dock_Es))]
 
    def linear_index_to_snapshot_index(ind):
      state_index = list(ind<cum_N_state).index(True)-1
      nis_index = ind-cum_N_state[state_index]
      cycle_index = list(nis_index<cum_N_cycle[state_index]).index(True)-1
      nic_index = nis_index-cum_N_cycle[state_index][cycle_index]
      return (state_index,cycle_index,nic_index)

    def snapshot_index_to_linear_index(state_index,cycle_index,nic_index):
      return cum_N_state[state_index]+cum_N_cycle[state_index][cycle_index]+nic_index

    # Terms to copy
    if self.params['dock']['pose'] > -1:
      # Pose BPMF
      terms = ['MM',\
        'k_angular_ext','k_spatial_ext','k_angular_int'] + self._scalables
    else:
      # BPMF
      terms = ['MM','site'] + self._scalables

    dock_Es_s = []
    confs_s = []
    for c in range(len(self.dock_Es[0])):
      dock_Es_c = dict([(term,[]) for term in terms])
      confs_c = []
      for n_in_c in range(len(self.dock_Es[-1][c]['MM'])):
        if (cum_N_cycle[-1][c]==0):
          (snapshot_s,snapshot_c,snapshot_n) = linear_index_to_snapshot_index(\
           np.random.choice(range(len(weights)), size = 1, p = weights)[0])
        else:
          snapshot_c = np.inf
          while (snapshot_c>c):
            (snapshot_s,snapshot_c,snapshot_n) = linear_index_to_snapshot_index(\
             np.random.choice(range(len(weights)), size = 1, p = weights)[0])
        for term in terms:
          dock_Es_c[term].append(\
            np.copy(self.dock_Es[snapshot_s][snapshot_c][term][snapshot_n]))
        if self.params['dock']['keep_intermediate']:
          # Has not been tested:
          confs_c.append(\
            np.copy(self.confs['dock']['samples'][snapshot_s][snapshot_c]))
      for term in terms:
        dock_Es_c[term] = np.array(dock_Es_c[term])
      dock_Es_s.append(dock_Es_c)
      confs_s.append(confs_c)
      
    # Insert resampled values
    self.dock_protocol.insert(neighbor_ind+1, lambda_n)
    self.dock_Es.insert(neighbor_ind+1, dock_Es_s)
    self.confs['dock']['samples'].insert(neighbor_ind+1, confs_s)
    self.confs['dock']['replicas'].insert(neighbor_ind+1, \
      np.copy(self.confs['dock']['replicas'][neighbor_ind]))

    if clear:
      self._clear_f_RL()

  def _insert_dock_state_between_low_acc(self):
    # Insert thermodynamic states between those with low acceptance probabilities
    eq_c = self._get_equilibrated_cycle('dock')[-1]
        
    def calc_mean_acc(k):
      dock_Es = [Es[eq_c:self._dock_cycle] for Es in self.dock_Es]
      (u_kln,N_k) = self._u_kln(dock_Es[k:k+2],\
                                self.dock_protocol[k:k+2])
      N = min(N_k)
      acc = np.exp(-u_kln[0,1,:N]-u_kln[1,0,:N]+u_kln[0,0,:N]+u_kln[1,1,:N])
      return np.mean(np.minimum(acc,np.ones(acc.shape)))

    updated = False
    k = 0
    while k<len(self.dock_protocol)-1:
      mean_acc = calc_mean_acc(k)
      # print k, self.dock_protocol[k]['a'], self.dock_protocol[k+1]['a'], mean_acc
      while mean_acc<0.4:
        if not updated:
          updated = True
          self._set_lock('dock')
        a_k = self.dock_protocol[k]['a']
        a_kp = self.dock_protocol[k+1]['a']
        a_n = (a_k+a_kp)/2.
        report =  '  inserted state'
        report += ' between %.5g and %.5g at %.5g\n'%(a_k,a_kp,a_n)
        report += '  to improve acceptance rate from %.5g '%mean_acc
        self._insert_dock_state(a_n, clear=False)
        mean_acc = calc_mean_acc(k)
        report += 'to %.5g'%mean_acc
        # print k, self.dock_protocol[k]['a'], self.dock_protocol[k+1]['a'], mean_acc
        self.tee(report)
      k += 1
    if updated:
      self._clear_f_RL()
      self._save('dock')
      self.tee("")
      self._clear_lock('dock')

  def _get_confs_to_rescore(self, nconfs=None, site=False, minimize=True, sort=True):
    """
    Returns configurations to rescore and their corresponding energies 
    as a tuple of lists, ordered by DECREASING energy.
    It is either the default configuration, or from dock6 and initial docking.
    If nconfs is None, then all configurations will be unique.
    If nconfs is smaller than the number of unique configurations, 
    then the lowest energy configurations will be retained.
    If nconfs is larger than the number of unique configurations, 
    then the lowest energy configuration will be duplicated.
    """
    # Get configurations
    count = {'xtal':0, 'dock6':0, 'initial_dock':0, 'duplicated':0}
    
    # based on the score option
    if self._FNs['score']=='default':
      confs = [np.copy(self.confs['ligand'])]
      count['xtal'] = 1
      Es = {}
      if nconfs is None:
        nconfs = 1
    elif (self._FNs['score'] is None) or (not os.path.isfile(self._FNs['score'])):
      confs = []
      Es = {}
    elif self._FNs['score'].endswith('.mol2') or \
         self._FNs['score'].endswith('.mol2.gz'):
      import AlGDock.IO
      IO_dock6_mol2 = AlGDock.IO.dock6_mol2()
      (confs, Es) = IO_dock6_mol2.read(self._FNs['score'], \
        reorder=self.molecule.inv_prmtop_atom_order,
        multiplier=0.1) # to convert Angstroms to nanometers
      count['dock6'] = len(confs)
    elif self._FNs['score'].endswith('.nc'):
      from netCDF4 import Dataset
      dock6_nc = Dataset(self._FNs['score'],'r')
      confs = [dock6_nc.variables['confs'][n][self.molecule.inv_prmtop_atom_order,:] for n in range(dock6_nc.variables['confs'].shape[0])]
      Es = dict([(key,dock6_nc.variables[key][:]) for key in dock6_nc.variables.keys() if key !='confs'])
      dock6_nc.close()
      count['dock6'] = len(confs)
    elif self._FNs['score'].endswith('.pkl.gz'):
      F = gzip.open(self._FNs['score'],'r')
      confs = pickle.load(F)
      F.close()
      if not isinstance(confs, list):
        confs = [confs]
      Es = {}
    else:
      raise Exception('Input configuration format not recognized')

    # based on the seeds
    if (self.confs['dock']['seeds'] is not None) and \
       (self.params['dock']['pose']==-1):
      confs = confs + self.confs['dock']['seeds']
      Es = {}
      count['initial_dock'] = len(self.confs['dock']['seeds'])

    if len(confs)==0:
      return ([],{})

    if site:
      # Filters out configurations not in the binding site
      confs_in_site = []
      Es_in_site = dict([(label,[]) for label in Es.keys()])
      old_eval = None
      if (None,None,None) in self.universe._evaluator.keys():
        old_eval = self.universe._evaluator[(None,None,None)]
      self._set_universe_evaluator({'site':True,'T':self.T_SIMMIN})
      for n in range(len(confs)):
        self.universe.setConfiguration(Configuration(self.universe, confs[n]))
        if self.universe.energy()<1.:
          confs_in_site.append(confs[n])
          for label in Es.keys():
            Es_in_site[label].append(Es[label][n])
      if old_eval is not None:
        self.universe._evaluator[(None,None,None)] = old_eval
      confs = confs_in_site
      Es = Es_in_site
      
    try:
      self.universe.energy()
    except ValueError:
      return (confs,{})

    if minimize:
      Es = {}
      from MMTK.Minimization import SteepestDescentMinimizer # @UnresolvedImport
      minimizer = SteepestDescentMinimizer(self.universe)

      original_stderr = sys.stderr
      sys.stderr = NullDevice() # Suppresses warnings for minimization

      minimized_confs = []
      minimized_energies = []
      self.start_times['minimization'] = time.time()
      for conf in confs:
        self.universe.setConfiguration(Configuration(self.universe, conf))
        x_o = np.copy(self.universe.configuration().array)
        e_o = self.universe.energy()
        for rep in range(50):
          minimizer(steps = 25)
          x_n = np.copy(self.universe.configuration().array)
          e_n = self.universe.energy()
          diff = abs(e_o-e_n)
          if np.isnan(e_n) or diff<0.05 or diff>1000.:
            self.universe.setConfiguration(Configuration(self.universe, x_o))
            break
          else:
            x_o = x_n
            e_o = e_n
        if not np.isnan(e_o):
          minimized_confs.append(x_o)
          minimized_energies.append(e_o)
    
      sys.stderr = original_stderr # Restores error reporting
      
      confs = minimized_confs
      energies = minimized_energies
      self.tee("  minimized %d configurations in "%len(confs) + \
        HMStime(time.time()-self.start_times['minimization']) + \
        "\n  the first %d energies are:\n  "%min(len(confs),10) + \
        ', '.join(['%.2f'%e for e in energies[:10]]))
    else:
      # Evaluate energies
      energies = []
      for conf in confs:
        self.universe.setConfiguration(Configuration(self.universe, conf))
        energies.append(self.universe.energy())

    if sort and len(confs)>0:
      # Sort configurations by DECREASING energy
      energies, confs = (list(l) for l in zip(*sorted(zip(energies, confs), \
        key=lambda p:p[0], reverse=True)))

    # Shrink or extend configuration and energy array
    if nconfs is not None:
      confs = confs[-nconfs:]
      energies = energies[-nconfs:]
      while len(confs)<nconfs:
        confs.append(confs[-1])
        energies.append(energies[-1])
        count['duplicated'] += 1
      count['nconfs'] = nconfs
    else:
      count['nconfs'] = len(confs)
    count['minimized'] = {True:' minimized', False:''}[minimize]
    Es['total'] = np.array(energies)

    self.tee("  keeping {nconfs}{minimized} configurations out of\n  {xtal} from xtal, {dock6} from dock6, {initial_dock} from initial docking, and {duplicated} duplicated".format(**count))
    return (confs, Es)

  def _run_MBAR(self,u_kln,N_k,augmented=False):
    """
    Estimates the free energy of a transition using BAR and MBAR
    """
    import pymbar
    K = len(N_k)-1 if augmented else len(N_k)
    f_k_FEPF = np.zeros(K)
    f_k_BAR = np.zeros(K)
    W_nl = None
    for k in range(K-1):
      w_F = u_kln[k,k+1,:N_k[k]] - u_kln[k,k,:N_k[k]]
      min_w_F = min(w_F)
      w_R = u_kln[k+1,k,:N_k[k+1]] - u_kln[k+1,k+1,:N_k[k+1]]
      min_w_R = min(w_R)
      f_k_FEPF[k+1] = -np.log(np.mean(np.exp(-w_F+min_w_F))) + min_w_F
      try:
        f_k_BAR[k+1] = pymbar.BAR(w_F, w_R, \
                       relative_tolerance=1.0E-5, \
                       verbose=False, \
                       compute_uncertainty=False)
      except:
        f_k_BAR[k+1] = f_k_FEPF[k+1]
        print 'Error with BAR. Using FEP.'
    f_k_FEPF = np.cumsum(f_k_FEPF)
    f_k_BAR = np.cumsum(f_k_BAR)
    try:
      if augmented:
        f_k_BAR = np.append(f_k_BAR,[0])
      f_k_pyMBAR = pymbar.MBAR(u_kln, N_k, \
        relative_tolerance=1.0E-5, \
        verbose = False, \
        initial_f_k = f_k_BAR, \
        maximum_iterations = 20)
      f_k_MBAR = f_k_pyMBAR.f_k
      W_nl = f_k_pyMBAR.getWeights()
    except:
      print N_k, f_k_BAR
      f_k_MBAR = f_k_BAR
      print 'Error with MBAR. Using BAR.'
    if np.isnan(f_k_MBAR).any():
      f_k_MBAR = f_k_BAR
      print 'Error with MBAR. Using BAR.'
    return (f_k_MBAR,W_nl)

  def _u_kln(self,eTs,lambdas,noBeta=False):
    """
    Computes a reduced potential energy matrix.  k is the sampled state.  l is the state for which energies are evaluated.
    
    Input:
    eT is a 
      -dictionary (of mapped energy terms) of numpy arrays (over states)
      -list (over states) of dictionaries (of mapped energy terms) of numpy arrays (over configurations), or a
      -list (over states) of lists (over cycles) of dictionaries (of mapped energy terms) of numpy arrays (over configurations)
    lambdas is a list of thermodynamic states
    noBeta means that the energy will not be divided by RT
    
    Output: u_kln or (u_kln, N_k)
    u_kln is the matrix (as a numpy array)
    N_k is an array of sample sizes
    """
    L = len(lambdas)

    addMM = ('MM' in lambdas[0].keys()) and (lambdas[0]['MM'])
    addSite = ('site' in lambdas[0].keys()) and (lambdas[0]['site'])
    probe_keys = ['MM','k_angular_ext','k_spatial_ext','k_angular_int'] + \
      self._scalables
    probe_key = [key for key in lambdas[0].keys() if key in probe_keys][0]
    
    if isinstance(eTs,dict):
      # There is one configuration per state
      K = len(eTs[probe_key])
      N_k = np.ones(K, dtype=int)
      u_kln = []
      E_base = np.zeros(K)
      if addMM:
        E_base += eTs['MM']
      if addSite:
        E_base += eTs['site']
      for l in range(L):
        E = 1.*E_base
        for scalable in self._scalables:
          if scalable in lambdas[l].keys():
            E += lambdas[l][scalable]*eTs[scalable]
        for key in ['k_angular_ext','k_spatial_ext','k_angular_int']:
          if key in lambdas[l].keys():
            E += lambdas[l][key]*eTs[key]
        if noBeta:
          u_kln.append(E)
        else:
          u_kln.append(E/(R*lambdas[l]['T']))
    elif isinstance(eTs[0],dict):
      K = len(eTs)
      N_k = np.array([len(eTs[k][probe_key]) for k in range(K)])
      u_kln = np.zeros([K, L, N_k.max()], np.float)

      for k in range(K):
        E_base = 0.0
        if addMM:
          E_base += eTs[k]['MM']
        if addSite:
          E_base += eTs[k]['site']          
        for l in range(L):
          E = 1.*E_base
          for scalable in self._scalables:
            if scalable in lambdas[l].keys():
              E += lambdas[l][scalable]*eTs[k][scalable]
          for key in ['k_angular_ext','k_spatial_ext','k_angular_int']:
            if key in lambdas[l].keys():
              E += lambdas[l][key]*eTs[k][key]
          if noBeta:
            u_kln[k,l,:N_k[k]] = E
          else:
            u_kln[k,l,:N_k[k]] = E/(R*lambdas[l]['T'])
    elif isinstance(eTs[0],list):
      K = len(eTs)
      N_k = np.zeros(K, dtype=int)

      for k in range(K):
        for c in range(len(eTs[k])):
          N_k[k] += len(eTs[k][c][probe_key])
      u_kln = np.zeros([K, L, N_k.max()], np.float)

      for k in range(K):
        E_base = 0.0
        C = len(eTs[k])
        if addMM:
          E_base += np.concatenate([eTs[k][c]['MM'] for c in range(C)])
        if addSite:
          E_base += np.concatenate([eTs[k][c]['site'] for c in range(C)])
        for l in range(L):
          E = 1.*E_base
          for scalable in self._scalables:
            if scalable in lambdas[l].keys():
              E += lambdas[l][scalable]*np.concatenate([eTs[k][c][scalable] \
                for c in range(C)])
          for key in ['k_angular_ext','k_spatial_ext','k_angular_int']:
            if key in lambdas[l].keys():
              E += lambdas[l][key]*np.concatenate([eTs[k][c][key] \
                for c in range(C)])
          if noBeta:
            u_kln[k,l,:N_k[k]] = E
          else:
            u_kln[k,l,:N_k[k]] = E/(R*lambdas[l]['T'])

    if (K==1) and (L==1):
      return u_kln.ravel()
    else:
      return (u_kln,N_k)

  def _next_cool_state(self, E=None, lambda_o=None, pow=None, warm=True):
    if E is None:
      E = self.cool_Es[-1]

    if lambda_o is None:
      lambda_o = self.cool_protocol[-1]

    if self.params['cool']['protocol'] == 'Adaptive':
      tL_tensor = self._tL_tensor(E,lambda_o,process='cool')
      crossed = lambda_o['crossed']
      if pow is not None:
        tL_tensor = tL_tensor*(1.25**pow)
      if tL_tensor>1E-7:
        dL = self.params['cool']['therm_speed']/tL_tensor
        if warm:
          T = lambda_o['T'] + dL
          if T > self.T_HIGH:
            T = self.T_HIGH
            crossed = True
        else:
          T = lambda_o['T'] - dL
          if T < self.T_SIMMIN:
            T = self.T_SIMMIN
            crossed = True
      else:
        raise Exception('No variance in configuration energies')
    elif self.params['cool']['protocol'] == 'Geometric':
      T_GEOMETRIC = np.exp(np.linspace(np.log(self.T_SIMMIN),np.log(self.T_HIGH),
        int(1/self.params['cool']['therm_speed'])))
      if warm:
        T_START, T_END = self.T_SIMMIN, self.T_HIGH
      else:
        T_START, T_END = self.T_HIGH, self.T_SIMMIN
        T_GEOMETRIC = T_GEOMETRIC[::-1]
      T = T_GEOMETRIC[len(self.cool_protocol)]
      crossed = (len(self.cool_protocol)==(len(T_GEOMETRIC)-1))
    a = (self.T_HIGH-T)/(self.T_HIGH-self.T_SIMMIN)
    return self._lambda(a, process='cool', lambda_o=lambda_o, crossed=crossed)

  def _next_dock_state(self, E=None, lambda_o=None, pow=None, undock=False):
    """
    Determines the parameters for the next docking state
    """
    
    if E is None:
      E = self.dock_Es[-1]

    if lambda_o is None:
      lambda_o = self.dock_protocol[-1]
    
    if self.params['dock']['protocol']=='Adaptive':
      # Change grid scaling and temperature simultaneously
      tL_tensor = self._tL_tensor(E,lambda_o)
      crossed = lambda_o['crossed']
      # Calculate the change in the progress variable, capping at 0.05
      if pow is None:
        dL = min(self.params['dock']['therm_speed']/tL_tensor, 0.05)
      else:
        # If there have been rejected stages, reduce dL
        dL = min(self.params['dock']['therm_speed']/tL_tensor/(1.25**pow), 0.05)
      if undock:
        a = lambda_o['a'] - dL
        if (self.params['dock']['pose'] > -1) and \
           (lambda_o['a'] > 0.5) and (a < 0.5):
          # Stop at 0.5 to facilitate entropy-energy decomposition
          a = 0.5
        elif a < 0.0:
          if pow>0:
            a = lambda_o['a']*(1-0.8**pow)
          else:
            a = 0.0
            crossed = True
      else:
        a = lambda_o['a'] + dL
        if (self.params['dock']['pose'] > -1) and \
           (lambda_o['a'] < 0.5) and (a > 0.5):
          # Stop at 0.5 to facilitate entropy-energy decomposition
          a = 0.5
        elif a > 1.0:
          if pow>0:
            a = lambda_o['a'] + (1-lambda_o['a'])*0.8**pow
          else:
            a = 1.0
            crossed = True
      return self._lambda(a, process='dock', lambda_o=lambda_o, crossed=crossed)
    elif self.params['dock']['protocol']=='Geometric':
      A_GEOMETRIC = [0.] + list(np.exp(np.linspace(np.log(1E-10),np.log(1.0),
        int(1/self.params['dock']['therm_speed']))))
      if undock:
        A_GEOMETRIC.reverse()
      a = A_GEOMETRIC[len(self.dock_protocol)]
      crossed = len(self.dock_protocol)==(len(A_GEOMETRIC)-1)
      return self._lambda(a, process='dock', lambda_o=lambda_o, crossed=crossed)

  def _tL_tensor(self, E, lambda_c, process='dock'):
    # Metric tensor for the thermodynamic length
    T = lambda_c['T']
    deltaT = np.abs(self.T_HIGH-self.T_SIMMIN)
    if process=='dock':
      a = lambda_c['a']
      a_g = 4.*(a-0.5)**2/(1+np.exp(-100*(a-0.5)))
      if a_g<1E-10:
        a_g=0
      da_g_da = (400.*(a-0.5)**2*np.exp(-100.*(a-0.5)))/(\
        1+np.exp(-100.*(a-0.5)))**2 + \
        (8.*(a-0.5))/(1 + np.exp(-100.*(a-0.5)))
    
      # Psi_g are terms thar are scaled in with a_g
      # OBC is the strength of the OBC scaling in the current state
      if self.params['dock']['solvation']=='Desolvated':
        Psi_g = self._u_kln([E], [{'LJr':1,'LJa':1,'ELE':1}], noBeta=True)
        OBC = 0.0
      elif self.params['dock']['solvation']=='Fractional':
        Psi_g = self._u_kln([E], [{'LJr':1,'LJa':1,'ELE':1,'OBC':1}], noBeta=True)
        OBC = a_g
      elif self.params['dock']['solvation']=='Full':
        Psi_g = self._u_kln([E], [{'LJr':1,'LJa':1,'ELE':1}], noBeta=True)
        OBC = 1.0

      if self.params['dock']['pose'] > -1: # Pose BPMF
        a_r = np.tanh(16*a*a)
        da_r_da = 38.*a/np.cosh(16.*a*a)**2
        U_r = self._u_kln([E], [{'k_angular_ext':self.params['dock']['k_pose'], \
          'k_spatial_ext':self.params['dock']['k_pose'], \
          'k_angular_int':self.params['dock']['k_pose']}], noBeta=True)
        U_RL_g = self._u_kln([E],
          [{'MM':True, 'OBC':OBC, 'T':T, \
            'k_angular_ext':lambda_c['k_angular_ext'], \
            'k_spatial_ext':lambda_c['k_spatial_ext'], \
            'k_angular_int':lambda_c['k_angular_int'], \
            'LJr':a_g, 'LJa':a_g, 'ELE':a_g}], noBeta=True)
        return np.abs(da_r_da)*U_r.std()/(R*T) + \
               np.abs(da_g_da)*Psi_g.std()/(R*T) + \
               deltaT*U_RL_g.std()/(R*T*T)
      else:
        # BPMF
        a_sg = 1.-4.*(a-0.5)**2
        da_sg_da = -8*(a-0.5)
        Psi_sg = self._u_kln([E], [{'sLJr':1,'sELE':1}], noBeta=True)
        U_RL_g = self._u_kln([E],
          [{'MM':True, 'OBC':OBC, 'site':True, 'T':T,\
          'sLJr':a_sg, 'sELE':a_sg, 'LJr':a_g, 'LJa':a_g, 'ELE':a_g}], noBeta=True)
        return np.abs(da_sg_da)*Psi_sg.std()/(R*T) + \
               np.abs(da_g_da)*Psi_g.std()/(R*T) + \
               deltaT*U_RL_g.std()/(R*T*T)
    elif process=='cool':
      if self.params['cool']['solvation']=='Full':
        # OBC is always on
        return deltaT*self._u_kln([E],[{'MM':True, 'OBC':1.0}], noBeta=True).std()/(R*T*T)
      else:
        # OBC is scaled with the progress variable
        a = lambda_c['a']
        return self._u_kln([E],[{'OBC':1.0}], noBeta=True).std()/(R*T) + \
          deltaT*self._u_kln([E],[{'MM':True, 'OBC':a}], noBeta=True).std()/(R*T*T)
    else:
      raise Exception("Unknown process!")

  def _lambda(self, a, process='dock', lambda_o=None, site=True, crossed=False):
    if (lambda_o is None) and len(getattr(self,process+'_protocol'))>0:
      lambda_o = copy.deepcopy(getattr(self,process+'_protocol')[-1])
    if (lambda_o is not None):
      lambda_n = copy.deepcopy(lambda_o)
      if 'steps_per_trial' not in lambda_o.keys():
        lambda_n['steps_per_trial'] = 1*self.params[process]['steps_per_sweep']
    else:
      lambda_n = {}
      lambda_n['steps_per_trial'] = 1*self.params[process]['steps_per_sweep']

    lambda_n['MM'] = True
    if crossed is not None:
      lambda_n['crossed'] = crossed

    if process=='dock':
      a_g = 4.*(a-0.5)**2/(1+np.exp(-100*(a-0.5)))
      if a_g<1E-10:
        a_g=0
      if self.params['dock']['solvation']=='Desolvated':
        lambda_n['OBC'] = 0
      elif self.params['dock']['solvation']=='Fractional':
        lambda_n['OBC'] = a_g # Scales the solvent with the grid
      elif self.params['dock']['solvation']=='Full':
        lambda_n['OBC'] = 1.0
      if self.params['dock']['pose'] > -1:
        # Pose BPMF
        a_r = np.tanh(16*a*a)
        lambda_n['a'] = a
        lambda_n['k_angular_int'] = self.params['dock']['k_pose']*a_r
        lambda_n['k_angular_ext'] = self.params['dock']['k_pose']
        lambda_n['k_spatial_ext'] = self.params['dock']['k_pose']
        lambda_n['LJr'] = a_g
        lambda_n['LJa'] = a_g
        lambda_n['ELE'] = a_g
        lambda_n['T'] = a_r*(self.T_SIMMIN-self.T_HIGH) + self.T_HIGH
      else:
        # BPMF
        a_sg = 1.-4.*(a-0.5)**2
        lambda_n['a'] = a
        lambda_n['sLJr'] = a_sg
        lambda_n['sELE'] = a_sg
        lambda_n['LJr'] = a_g
        lambda_n['LJa'] = a_g
        lambda_n['ELE'] = a_g
        if site is not None:
          lambda_n['site'] = site
        if self.params['dock']['temperature_scaling']=='Linear':
          lambda_n['T'] = a*(self.T_SIMMIN-self.T_HIGH) + self.T_HIGH
        elif self.params['dock']['temperature_scaling']=='Quadratic':
          lambda_n['T'] = a_g*(self.T_SIMMIN-self.T_HIGH) + self.T_HIGH
    elif process=='cool':
      lambda_n['a'] = a
      lambda_n['T'] = self.T_HIGH - a*(self.T_HIGH-self.T_SIMMIN)
      if self.params['cool']['solvation']=='Desolvated':
        lambda_n['OBC'] = a
      elif self.params['cool']['solvation']=='Fractional':
        lambda_n['OBC'] = a
      elif self.params['cool']['solvation']=='Full':
        lambda_n['OBC'] = 1.0
    else:
      raise Exception("Unknown process!")

    return lambda_n

  def _load_programs(self, phases):
    # Find the necessary programs, downloading them if necessary
    programs = []
    for phase in phases:
      for (prefix,program) in [('NAMD','namd'), \
          ('sander','sander'), ('gbnsr6','gbnsr6'), ('APBS','apbs')]:
        if phase.startswith(prefix) and not program in programs:
          programs.append(program)
      if phase.find('ALPB')>-1:
        if not 'elsize' in programs:
          programs.append('elsize')
        if not 'ambpdb' in programs:
          programs.append('ambpdb')
    if 'apbs' in programs:
      for program in ['ambpdb','molsurf']:
        if not program in programs:
          programs.append(program)
    for program in programs:
      self._FNs[program] = a.findPaths([program])[program]
    a.loadModules(programs)

  def _postprocess(self,
      conditions=[('original',0, 0,'R'), ('cool',-1,-1,'L'), \
                  ('dock',   -1,-1,'L'), ('dock',-1,-1,'RL')],
      phases=None,
      readOnly=False, redo_dock=False, debug=DEBUG):
    """
    Obtains the NAMD energies of all the conditions using all the phases.  
    Saves both MMTK and NAMD energies after NAMD energies are estimated.
    
    state == -1 means the last state
    cycle == -1 means all cycles

    """
    # Clear evaluators to save memory
    self._evaluators = {}
    
    if phases is None:
      phases = list(set(self.params['cool']['phases'] + \
        self.params['dock']['phases']))

    updated_processes = []

    # Identify incomplete calculations
    incomplete = []
    for (p, state, cycle, moiety) in conditions:
      # Check that the values are legitimate
      if not p in ['cool','dock','original']:
        raise Exception("Type should be in ['cool', 'dock', 'original']")
      if not moiety in ['R','L', 'RL']:
        raise Exception("Species should in ['R','L', 'RL']")
      if p!='original' and getattr(self,p+'_protocol')==[]:
        continue
      if state==-1:
        state = len(getattr(self,p+'_protocol'))-1
      if cycle==-1:
        cycles = range(getattr(self,'_'+p+'_cycle'))
      else:
        cycles = [cycle]

      # Check for completeness
      for c in cycles:
        for phase in phases:
          label = moiety+phase
          
          # Skip postprocessing
          # if the function is NOT being rerun in redo_dock mode
          # and one of the following:
          # the function is being run in readOnly mode,
          # the energies are already in memory.
          if (not (redo_dock and p=='dock')) and \
            (readOnly \
            or (p == 'original' and \
                (label in getattr(self,p+'_Es')[state][c].keys()) and \
                (getattr(self,p+'_Es')[state][c][label] is not None)) \
            or (('MM' in getattr(self,p+'_Es')[state][c].keys()) and \
                (label in getattr(self,p+'_Es')[state][c].keys()) and \
                (len(getattr(self,p+'_Es')[state][c]['MM'])==\
                 len(getattr(self,p+'_Es')[state][c][label])))):
            pass
          else:
            incomplete.append((p, state, c, moiety, phase))

    if incomplete==[]:
      return True
    
    del p, state, c, moiety, phase, cycles, label
    
    self._load_programs([val[-1] for val in incomplete])

    # Write trajectories and queue calculations
    m = multiprocessing.Manager()
    task_queue = m.Queue()
    time_per_snap = m.dict()
    for (p, state, c, moiety, phase) in incomplete:
      if moiety+phase not in time_per_snap.keys():
        time_per_snap[moiety+phase] = m.list()

    # Decompress prmtop and inpcrd files
    decompress = (self._FNs['prmtop'][moiety].endswith('.gz')) or \
                 (self._FNs['inpcrd'][moiety].endswith('.gz'))
    if decompress:
      for key in ['prmtop','inpcrd']:
        if self._FNs[key][moiety].endswith('.gz'):
          import shutil
          shutil.copy(self._FNs[key][moiety],self._FNs[key][moiety]+'.BAK')
          os.system('gunzip -f '+self._FNs[key][moiety])
          os.rename(self._FNs[key][moiety]+'.BAK', self._FNs[key][moiety])
          self._FNs[key][moiety] = self._FNs[key][moiety][:-3]

    toClean = []

    for (p, state, c, moiety, phase) in incomplete:
      # Identify the configurations
      if (moiety=='R'):
        if not 'receptor' in self.confs.keys():
          continue
        confs = [self.confs['receptor']]
      else:
        confs = self.confs[p]['samples'][state][c]

      # Identify the file names
      if p=='original':
        prefix = p
      else:
        prefix = '%s%d_%d'%(p, state, c)

      p_dir = {'cool':self.dir['cool'],
         'original':self.dir['dock'],
         'dock':self.dir['dock']}[p]
      
      if phase.startswith('NAMD'):
        traj_FN = os.path.join(p_dir,'%s.%s.dcd'%(prefix,moiety))
      elif phase.startswith('sander'):
        traj_FN = os.path.join(p_dir,'%s.%s.mdcrd'%(prefix,moiety))
      elif phase.startswith('gbnsr6'):
        traj_FN = os.path.join(p_dir,'%s.%s%s'%(prefix,moiety,phase),'in.crd')
      elif phase.startswith('OpenMM'):
        traj_FN = None
      elif phase in ['APBS_PBSA']:
        traj_FN = os.path.join(p_dir,'%s.%s.pqr'%(prefix,moiety))
      outputname = os.path.join(p_dir,'%s.%s%s'%(prefix,moiety,phase))

      # Writes trajectory
      self._write_traj(traj_FN, confs, moiety)
      if (traj_FN is not None) and (not traj_FN in toClean):
        toClean.append(traj_FN)

      # Queues the calculations
      task_queue.put((confs, moiety, phase, traj_FN, outputname, debug, \
              (p,state,c,moiety+phase)))

    # Start postprocessing
    self._set_lock('dock' if 'dock' in [loc[0] for loc in incomplete] else 'cool')
    self.tee("\n>>> Postprocessing, starting at " + \
      time.strftime("%a, %d %b %Y %H:%M:%S", time.localtime()) + "\n")
    self.start_times['postprocess'] = time.time()

    done_queue = m.Queue()
    processes = [multiprocessing.Process(target=self._energy_worker, \
        args=(task_queue, done_queue, time_per_snap)) \
        for p in range(self._cores)]
    for p in range(self._cores):
      task_queue.put('STOP')
    for p in processes:
      p.start()
    for p in processes:
      p.join()
    results = []
    while not done_queue.empty():
      results.append(done_queue.get())
    for p in processes:
      p.terminate()

    # Clean up files
    if not debug:
      for FN in toClean:
        if os.path.isfile(FN):
          os.remove(FN)

    # Clear decompressed files
    if decompress:
      for key in ['prmtop','inpcrd']:
        if os.path.isfile(self._FNs[key][moiety]+'.gz'):
          os.remove(self._FNs[key][moiety])
          self._FNs[key][moiety] = self._FNs[key][moiety] + '.gz'

    # Store energies
    updated_energy_dicts = []
    for (E,(p,state,c,label),wall_time) in results:
      if p=='original':
        self.original_Es[state][c][label] = E
        updated_energy_dicts.append(self.original_Es[state][c])
      else:
        getattr(self,p+'_Es')[state][c][label] = E
        updated_energy_dicts.append(getattr(self,p+'_Es')[state][c])
      if not p in updated_processes:
        updated_processes.append(p)
    for d in updated_energy_dicts:
      self._combine_MM_and_solvent(d)

    # Print time per snapshot
    for key in time_per_snap.keys():
      if len(time_per_snap[key])>0:
        mean_time_per_snap = np.mean(time_per_snap[key])
        if not np.isnan(mean_time_per_snap):
          self.tee("  an average of %.5g s per %s snapshot"%(\
            mean_time_per_snap, key))
        else:
          self.tee("  time per snapshot in %s: "%(key) + \
            ', '.join(['%.5g'%t for t in time_per_snap[key]]))
      else:
        self.tee("  no snapshots postprocessed in %s"%(key))

    # Save data
    if 'original' in updated_processes:
      for phase in phases:
        if (self.params['dock']['receptor_'+phase] is None) and \
           (self.original_Es[0][0]['R'+phase] is not None):
          self.params['dock']['receptor_'+phase] = \
            self.original_Es[0][0]['R'+phase]
      self._save('dock', keys=['progress'])
    if 'cool' in updated_processes:
      self._save('cool')
    if ('dock' in updated_processes) or ('original' in updated_processes):
      self._save('dock')

    if len(updated_processes)>0:
      self._clear_lock('dock' if 'dock' in updated_processes else 'cool')
      self.tee("\nElapsed time for postprocessing was " + \
        HMStime(time.time()-self.start_times['postprocess']))
      return len(incomplete)==len(results)

  def _energy_worker(self, input, output, time_per_snap):
    for args in iter(input.get, 'STOP'):
      (confs, moiety, phase, traj_FN, outputname, debug, reference) = args
      (p, state, c, label) = reference
      nsnaps = len(confs)
      
      # Make sure there is enough time remaining
      if self._run_type=='timed':
        remaining_time = self.timings['max']*60 - \
          (time.time()-self.start_times['run'])
        if len(time_per_snap[moiety+phase])>0:
          mean_time_per_snap = np.mean(np.mean(time_per_snap[moiety+phase]))
          if np.isnan(mean_time_per_snap):
            return
          projected_time = mean_time_per_snap*nsnaps
          self.tee("  projected cycle time for %s: %s, remaining time: %s"%(\
            moiety+phase, \
            HMStime(projected_time), HMStime(remaining_time)), process=p)
          if projected_time > remaining_time:
            return
    
      # Calculate the energy
      self.start_times['energy'] = time.time()
      for program in ['NAMD','sander','gbnsr6','OpenMM','APBS']:
        if phase.startswith(program):
          E = getattr(self,'_%s_Energy'%program)(*args)
          break
      wall_time = time.time() - self.start_times['energy']

      if not np.isinf(E).any():
        self.tee("  postprocessed %s, state %d, cycle %d, %s in %s"%(\
          p,state,c,label,HMStime(wall_time)))
          
        # Store output and timings
        output.put((E, reference, wall_time))

        times_per_snap = time_per_snap[moiety+phase]
        times_per_snap.append(wall_time/nsnaps)
        time_per_snap[moiety+phase] = times_per_snap
      else:
        self.tee("  error in postprocessing %s, state %d, cycle %d, %s in %s"%(\
          p,state,c,label,HMStime(wall_time)))
        return

  def _energyTerms(self, confs, E=None, process='dock', debug=DEBUG):
    """
    Calculates MMTK energy terms for a series of configurations
    Units are the MMTK standard, kJ/mol
    """
    if E is None:
      E = {}

    lambda_full = self._lambda(a=1.0, process=process, site=(process=='dock'))
    if process=='dock':
      for scalable in self._scalables:
        lambda_full[scalable] = 1
    self._set_universe_evaluator(lambda_full)

    # Molecular mechanics and grid interaction energies
    E['MM'] = np.zeros(len(confs), dtype=float)
    if process=='cool':
      if 'OBC' in lambda_full.keys():
        E['OBC'] = np.zeros(len(confs), dtype=float)
    if process=='dock':
      for term in (self._scalables):
        E[term] = np.zeros(len(confs), dtype=float)
      if 'site' in self._forceFields.keys():
        E['site'] = np.zeros(len(confs), dtype=float)
      if 'InternalRestraint' in self._forceFields.keys():
        E['k_angular_int'] = np.zeros(len(confs), dtype=float)
      if 'ExternalRestraint' in self._forceFields.keys():
        E['k_angular_ext'] = np.zeros(len(confs), dtype=float)
        E['k_spatial_ext'] = np.zeros(len(confs), dtype=float)
    for c in range(len(confs)):
      self.universe.setConfiguration(Configuration(self.universe,confs[c]))
      eT = self.universe.energyTerms()
      for (key,value) in eT.iteritems():
        if key=='electrostatic':
          pass # For some reason, MMTK double-counts electrostatic energies
        elif key.startswith('pose'):
          # For pose restraints, the energy is per spring constant unit
          E[term_map[key]][c] += value/lambda_full[term_map[key]]
        else:
          try:
            E[term_map[key]][c] += value
          except KeyError:
            print key
            print 'Keys in eT', eT.keys()
            print 'Keys in term map', term_map.keys()
            print 'Keys in E', E.keys()
            raise Exception('key not found in term map or E')
    return E
  
  def _NAMD_Energy(self, confs, moiety, phase, dcd_FN, outputname,
      debug=DEBUG, reference=None):
    """
    Uses NAMD to calculate the energy of a set of configurations
    Units are the MMTK standard, kJ/mol
    """
    # NAMD ENERGY FIELDS:
    # 0. TS 1. BOND 2. ANGLE 3. DIHED 4. IMPRP 5. ELECT 6. VDW 7. BOUNDARY
    # 8. MISC 9. KINETIC 10. TOTAL 11. TEMP 12. POTENTIAL 13. TOTAL3 14. TEMPAVG
    # The saved fields are energyFields=[1, 2, 3, 4, 5, 6, 8, 12],
    # and thus the new indicies are
    # 0. BOND 1. ANGLE 2. DIHED 3. IMPRP 4. ELECT 5. VDW 6. MISC 7. POTENTIAL
    
    # Run NAMD
    import AlGDock.NAMD
    energyCalc = AlGDock.NAMD.NAMD(\
      prmtop=self._FNs['prmtop'][moiety], \
      inpcrd=self._FNs['inpcrd'][moiety], \
      fixed={'R':self._FNs['fixed_atoms']['R'], \
             'L':None, \
             'RL':self._FNs['fixed_atoms']['RL']}[moiety], \
      solvent={'NAMD_OBC':'GBSA', 'NAMD_Gas':'Gas'}[phase], \
      useCutoff=(phase=='NAMD_OBC'), \
      namd_command=self._FNs['namd'])
    E = energyCalc.energies_PE(\
      outputname, dcd_FN, energyFields=[1, 2, 3, 4, 5, 6, 8, 12], \
      keepScript=debug, write_energy_pkl_gz=False)

    return np.array(E, dtype=float)*MMTK.Units.kcal/MMTK.Units.mol

  def _sander_Energy(self, confs, moiety, phase, AMBER_mdcrd_FN, \
      outputname=None, debug=DEBUG, reference=None):
    self.dir['out'] = os.path.dirname(os.path.abspath(AMBER_mdcrd_FN))
    script_FN = '%s%s.in'%('.'.join(AMBER_mdcrd_FN.split('.')[:-1]),phase)
    out_FN = '%s%s.out'%('.'.join(AMBER_mdcrd_FN.split('.')[:-1]),phase)

    script_F = open(script_FN,'w')
    script_F.write('''Calculating energies with sander
&cntrl
  imin=5,    ! read trajectory in for analysis
  ntx=1,     ! input is read formatted with no velocities
  irest=0,
  ntb=0,     ! no periodicity and no PME
  idecomp=0, ! no decomposition
  ntc=1,     ! No SHAKE
  cut=9999., !''')
    if phase=='sander_Gas':
      script_F.write("""
  ntf=1,     ! Complete interaction is calculated
/
""")
    elif phase=='sander_PBSA':
      fillratio = 4.0 if moiety=='L' else 2.0
      script_F.write('''
  ntf=7,     ! No bond, angle, or dihedral forces calculated
  ipb=2,     ! Default PB dielectric model
  inp=2,     ! non-polar from cavity + dispersion
/
&pb
  radiopt=0, ! Use atomic radii from the prmtop file
  fillratio=%d,
  sprob=1.4,
  cavity_surften=0.0378, ! (kcal/mol) Default in MMPBSA.py
  cavity_offset=-0.5692, ! (kcal/mol) Default in MMPBSA.py
/
'''%fillratio)
    else:
      if phase.find('ALPB')>-1 and moiety.find('R')>-1:
        script_F.write("\n  alpb=1,")
        script_F.write("\n  arad=%.2f,"%self.elsize)
      key = phase.split('_')[-1]
      igb = {'HCT':1, 'OBC1':2, 'OBC2':5, 'GBn':7, 'GBn2':8}[key]
      script_F.write('''
  ntf=7,     ! No bond, angle, or dihedral forces calculated
  igb=%d,     !
  gbsa=2,    ! recursive surface area algorithm (for postprocessing)
/
'''%(igb))
    script_F.close()
    
    os.chdir(self.dir['out'])
    import subprocess
    args_list = [self._FNs['sander'], '-O','-i',script_FN,'-o',out_FN, \
      '-p',self._FNs['prmtop'][moiety],'-c',self._FNs['inpcrd'][moiety], \
      '-y', AMBER_mdcrd_FN, '-r',script_FN+'.restrt']
    if debug:
      print ' '.join(args_list)
    p = subprocess.Popen(args_list)
    p.wait()
    
    F = open(out_FN,'r')
    dat = F.read().strip().split(' BOND')
    F.close()

    dat.pop(0)
    if len(dat)>0:
      # For the different models, all the terms are the same except for
      # EGB/EPB (every model is different)
      # ESURF versus ECAVITY + EDISPER
      # EEL (ALPB versus not)
      E = np.array([rec[:rec.find('\nminimization')].replace('1-4 ','1-4').split()[1::3] for rec in dat],dtype=float)*MMTK.Units.kcal/MMTK.Units.mol
      if phase=='sander_Gas':
        E = np.hstack((E,np.sum(E,1)[...,None]))
      else:
        # Mark as nan to add the Gas energies later
        E = np.hstack((E,np.ones((E.shape[0],1))*np.nan))

      if not debug and os.path.isfile(script_FN):
        os.remove(script_FN)
      if os.path.isfile(script_FN+'.restrt'):
        os.remove(script_FN+'.restrt')

      if not debug and os.path.isfile(out_FN):
        os.remove(out_FN)
    else:
      E = np.array([np.inf]*11)

    os.chdir(self.dir['start'])
    return E
    # AMBER ENERGY FIELDS:
    # For Gas phase:
    # 0. BOND 1. ANGLE 2. DIHEDRAL 3. VDWAALS 4. EEL
    # 5. HBOND 6. 1-4 VWD 7. 1-4 EEL 8. RESTRAINT
    # For GBSA phases:
    # 0. BOND 1. ANGLE 2. DIHEDRAL 3. VDWAALS 4. EEL
    # 5. EGB 6. 1-4 VWD 7. 1-4 EEL 8. RESTRAINT 9. ESURF
    # For PBSA phase:
    # 0. BOND 1. ANGLE 2. DIHEDRAL 3. VDWAALS 4. EEL
    # 5. EPB 6. 1-4 VWD 7. 1-4 EEL 8. RESTRAINT 9. ECAVITY 10. EDISPER

  def _get_elsize(self):
    # Calculates the electrostatic size of the receptor for ALPB calculations
    # Writes the coordinates in AMBER format
    pqr_FN = os.path.join(self.dir['dock'], 'receptor.pqr')
    if not os.path.isdir(self.dir['dock']):
      os.system('mkdir -p '+self.dir['dock'])
    
    import AlGDock.IO
    IO_crd = AlGDock.IO.crd()
    factor = 1.0/MMTK.Units.Ang
    IO_crd.write(self._FNs['inpcrd']['R'], factor*self.confs['receptor'], \
      'title', trajectory=False)
    
    # Converts the coordinates to a pqr file
    inpcrd_F = open(self._FNs['inpcrd']['R'],'r')
    cdir = os.getcwd()
    import subprocess
    try:
      p = subprocess.Popen(\
        [self._FNs['ambpdb'], \
         '-p', os.path.relpath(self._FNs['prmtop']['R'], cdir), \
         '-pqr'], \
        stdin=inpcrd_F, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
      (stdoutdata_ambpdb, stderrdata_ambpdb) = p.communicate()
      p.wait()
    except OSError:
      os.system('ls -ltr')
      print 'Command: ' + ' '.join([os.path.relpath(self._FNs['ambpdb'], cdir), \
         '-p', os.path.relpath(self._FNs['prmtop']['R'], cdir), \
         '-pqr'])
      print 'stdout:\n' + stdoutdata_ambpdb
      print 'stderr:\n' + stderrdata_ambpdb
    inpcrd_F.close()
    
    pqr_F = open(pqr_FN,'w')
    pqr_F.write(stdoutdata_ambpdb)
    pqr_F.close()

    # Runs the pqr file through elsize
    p = subprocess.Popen(\
      [self._FNs['elsize'], os.path.relpath(pqr_FN, cdir)], \
      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (stdoutdata_elsize, stderrdata_elsize) = p.communicate()
    p.wait()
    
    for FN in [pqr_FN]:
      if os.path.isfile(FN):
        os.remove(FN)
    try:
      elsize = float(stdoutdata_elsize.strip())
    except ValueError:
      print 'Command: ' + ' '.join([os.path.relpath(self._FNs['elsize'], cdir), \
       os.path.relpath(pqr_FN, cdir)])
      print stdoutdata_elsize
      print 'Error with elsize'
    return elsize

  def _gbnsr6_Energy(self, confs, moiety, phase, inpcrd_FN, outputname,
      debug=DEBUG, reference=None):
    """
    Uses gbnsr6 (part of AmberTools) 
    to calculate the energy of a set of configurations
    """
    # Prepare configurations for writing to crd file
    factor=1.0/MMTK.Units.Ang
    if (moiety.find('R')>-1):
      receptor_0 = factor*self.confs['receptor'][:self._ligand_first_atom,:]
      receptor_1 = factor*self.confs['receptor'][self._ligand_first_atom:,:]

    if not isinstance(confs,list):
      confs = [confs]
    
    if (moiety.find('R')>-1):
      if (moiety.find('L')>-1):
        full_confs = [np.vstack((receptor_0, \
          conf[self.molecule.prmtop_atom_order,:]/MMTK.Units.Ang, \
          receptor_1)) for conf in confs]
      else:
        full_confs = [factor*self.confs['receptor']]
    else:
      full_confs = [conf[self.molecule.prmtop_atom_order,:]/MMTK.Units.Ang \
        for conf in confs]

    # Set up directory
    inpcrdFN = os.path.abspath(inpcrd_FN)
    gbnsr6_dir = os.path.dirname(inpcrd_FN)
    os.system('mkdir -p '+gbnsr6_dir)
    os.chdir(gbnsr6_dir)
    cdir = os.getcwd()
    
    # Write gbnsr6 script
    chagb = 0 if phase.find('Still')>-1 else 1
    alpb = 1 if moiety.find('R')>-1 else 0 # ALPB ineffective with small solutes
    gbnsr6_in_FN = moiety+'gbnsr6.in'
    gbnsr6_in_F = open(gbnsr6_in_FN,'w')
    gbnsr6_in_F.write("""gbnsr6
&cntrl
  inp=1
/
&gb
  alpb=%d,
  chagb=%d
/
"""%(alpb, chagb))
    gbnsr6_in_F.close()

    args_list = [self._FNs['gbnsr6'], \
      '-i', os.path.relpath(gbnsr6_in_FN, cdir), \
      '-o', 'stdout', \
      '-p', os.path.relpath(self._FNs['prmtop'][moiety], cdir), \
      '-c', os.path.relpath(inpcrd_FN, cdir)]
    if debug:
      print ' '.join(args_list)

    # Write coordinates, run gbnsr6, and store energies
    import subprocess
    import AlGDock.IO
    IO_crd = AlGDock.IO.crd()

    E = []
    for full_conf in full_confs:
      # Writes the coordinates in AMBER format
      IO_crd.write(inpcrd_FN, full_conf, 'title', trajectory=False)
      
      # Runs gbnsr6
      import subprocess
      p = subprocess.Popen(args_list, \
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
      (stdoutdata, stderrdata) = p.communicate()
      p.wait()

      recs = stdoutdata.strip().split(' BOND')
      if len(recs)>1:
        rec = recs[1]
        E.append(rec[:rec.find('\n -----')].replace('1-4 ','1-4').split()[1::3])
      else:
        self.tee("  error has occured in gbnsr6 after %d snapshots"%len(E))
        self.tee("  prmtop was "+self._FNs['prmtop'][moiety])
        self.tee("  --- stdout:")
        self.tee(stdoutdata)
        self.tee("  --- stderr:")
        self.tee(stderrdata)
      
    E = np.array(E, dtype=float)*MMTK.Units.kcal/MMTK.Units.mol
    E = np.hstack((E,np.ones((E.shape[0],1))*np.nan))
    
    os.chdir(self.dir['start'])
    if not debug:
      os.system('rm -rf '+gbnsr6_dir)
    return E
    # For gbnsr6 phases:
    # 0. BOND 1. ANGLE 2. DIHED 3. 1-4 NB 4. 1-4 EEL
    # 5. VDWAALS 6. EELEC 7. EGB 8. RESTRAINT 9. ESURF
    
  def _OpenMM_Energy(self, confs, moiety, phase, traj_FN=None, \
      outputname=None, debug=DEBUG, reference=None):
    import simtk.openmm
    import simtk.openmm.app as OpenMM_app
    # Set up the simulation
    key = moiety+phase
    if not key in self._OpenMM_sims.keys():
      prmtop = OpenMM_app.AmberPrmtopFile(self._FNs['prmtop'][moiety])
      inpcrd = OpenMM_app.AmberInpcrdFile(self._FNs['inpcrd'][moiety])
      OMM_system = prmtop.createSystem(nonbondedMethod=OpenMM_app.NoCutoff, \
        constraints=None, implicitSolvent={
          'OpenMM_Gas':None,
          'OpenMM_GBn':OpenMM_app.GBn,
          'OpenMM_GBn2':OpenMM_app.GBn2,
          'OpenMM_HCT':OpenMM_app.HCT,
          'OpenMM_OBC1':OpenMM_app.OBC1,
          'OpenMM_OBC2':OpenMM_app.OBC2}[phase])
      dummy_integrator = simtk.openmm.LangevinIntegrator(300*simtk.unit.kelvin, \
        1/simtk.unit.picosecond, 0.002*simtk.unit.picoseconds)
      # platform = simtk.openmm.Platform.getPlatformByName('CPU')
      self._OpenMM_sims[key] = OpenMM_app.Simulation(prmtop.topology, \
        OMM_system, dummy_integrator)

    # Prepare the conformations by combining with the receptor if necessary
    if (moiety.find('R')>-1):
      receptor_0 = self.confs['receptor'][:self._ligand_first_atom,:]
      receptor_1 = self.confs['receptor'][self._ligand_first_atom:,:]
    if not isinstance(confs,list):
      confs = [confs]
    if (moiety.find('R')>-1):
      if (moiety.find('L')>-1):
        confs = [np.vstack((receptor_0, \
          conf[self.molecule.prmtop_atom_order,:], \
          receptor_1)) for conf in confs]
      else:
        confs = [self.confs['receptor']]
    else:
      confs = [conf[self.molecule.prmtop_atom_order,:] for conf in confs]
    
    # Calculate the energies
    E = []
    for conf in confs:
      self._OpenMM_sims[key].context.setPositions(conf)
      s = self._OpenMM_sims[key].context.getState(getEnergy=True)
      E.append([0., s.getPotentialEnergy()/simtk.unit.kilojoule*simtk.unit.mole])
    return np.array(E, dtype=float)*MMTK.Units.kJ/MMTK.Units.mol

  def _APBS_Energy(self, confs, moiety, phase, pqr_FN, outputname,
      debug=DEBUG, reference=None):
    """
    Uses APBS to calculate the solvation energy of a set of configurations
    Units are the MMTK standard, kJ/mol
    """
    # Prepare configurations for writing to crd file
    factor=1.0/MMTK.Units.Ang
    if (moiety.find('R')>-1):
      receptor_0 = factor*self.confs['receptor'][:self._ligand_first_atom,:]
      receptor_1 = factor*self.confs['receptor'][self._ligand_first_atom:,:]

    if not isinstance(confs,list):
      confs = [confs]
    
    if (moiety.find('R')>-1):
      if (moiety.find('L')>-1):
        full_confs = [np.vstack((receptor_0, \
          conf[self.molecule.prmtop_atom_order,:]/MMTK.Units.Ang, \
          receptor_1)) for conf in confs]
      else:
        full_confs = [factor*self.confs['receptor']]
    else:
      full_confs = [conf[self.molecule.prmtop_atom_order,:]/MMTK.Units.Ang \
        for conf in confs]

    # Write coordinates, run APBS, and store energies
    apbs_dir = os.path.abspath(pqr_FN)[:-4]
    os.system('mkdir -p '+apbs_dir)
    os.chdir(apbs_dir)
    pqr_FN = os.path.join(apbs_dir, 'in.pqr')

    import subprocess
    import AlGDock.IO
    IO_crd = AlGDock.IO.crd()

    E = []
    for full_conf in full_confs:
      # Writes the coordinates in AMBER format
      inpcrd_FN = pqr_FN[:-4]+'.crd'
      IO_crd.write(inpcrd_FN, full_conf, 'title', trajectory=False)
      
      # Converts the coordinates to a pqr file
      inpcrd_F = open(inpcrd_FN,'r')
      cdir = os.getcwd()
      p = subprocess.Popen(\
        [os.path.relpath(self._FNs['ambpdb'], cdir), \
         '-p', os.path.relpath(self._FNs['prmtop'][moiety], cdir), \
         '-pqr'], \
        stdin=inpcrd_F, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
      (stdoutdata_ambpdb, stderrdata_ambpdb) = p.communicate()
      p.wait()
      inpcrd_F.close()
      
      pqr_F = open(pqr_FN,'w')
      pqr_F.write(stdoutdata_ambpdb)
      pqr_F.close()
      
      # Writes APBS script
      apbs_in_FN = moiety+'apbs-mg-manual.in'
      apbs_in_F = open(apbs_in_FN,'w')
      apbs_in_F.write('READ\n  mol pqr {0}\nEND\n'.format(pqr_FN))

      for sdie in [80.0,1.0]:
        if moiety=='L':
          min_xyz = np.array([min(full_conf[a,:]) for a in range(3)])
          max_xyz = np.array([max(full_conf[a,:]) for a in range(3)])
          mol_range = max_xyz - min_xyz
          mol_center = (min_xyz + max_xyz)/2.
          
          def roundUpDime(x):
            return (np.ceil((x.astype(float)-1)/32)*32+1).astype(int)
          
          focus_spacing = 0.5
          focus_dims = roundUpDime(mol_range*LFILLRATIO/focus_spacing)
          args = zip(['mdh'],[focus_dims],[mol_center],[focus_spacing])
        else:
          args = zip(['mdh','focus'],
            self._apbs_grid['dime'], self._apbs_grid['gcent'],
            self._apbs_grid['spacing'])
        for (bcfl,dime,gcent,grid) in args:
          apbs_in_F.write('''ELEC mg-manual
  bcfl {0} # multiple debye-huckel boundary condition
  chgm spl4 # quintic B-spline charge discretization
  dime {1[0]} {1[1]} {1[2]}
  gcent {2[0]} {2[1]} {2[2]}
  grid {3} {3} {3}
  lpbe # Linearized Poisson-Boltzmann
  mol 1
  pdie 1.0
  sdens 10.0
  sdie {4}
  srad 1.4
  srfm smol # Smoothed dielectric and ion-accessibility coefficients
  swin 0.3
  temp 300.0
  calcenergy total
END
'''.format(bcfl,dime,gcent,grid,sdie))
      apbs_in_F.write('quit\n')
      apbs_in_F.close()

      # Runs APBS
#      TODO: Control the number of threads. This doesn't seem to do anything.
#      if self._cores==1:
#        os.environ['OMP_NUM_THREADS']='1'
      p = subprocess.Popen([self._FNs['apbs'], apbs_in_FN], \
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
      (stdoutdata, stderrdata) = p.communicate()
      p.wait()

      apbs_energy = [float(line.split('=')[-1][:-7]) \
        for line in stdoutdata.split('\n') \
        if line.startswith('  Total electrostatic energy')]
      if moiety=='L' and len(apbs_energy)==2:
        polar_energy = apbs_energy[0]-apbs_energy[1]
      elif len(apbs_energy)==4:
        polar_energy = apbs_energy[1]-apbs_energy[3]
      else:
        # An error has occured in APBS
        polar_energy = np.inf
        self.tee("  error has occured in APBS after %d snapshots"%len(E))
        self.tee("  prmtop was "+self._FNs['prmtop'][moiety])
        self.tee("  --- ambpdb stdout:")
        self.tee(stdoutdata_ambpdb)
        self.tee("  --- ambpdb stderr:")
        self.tee(stderrdata_ambpdb)
        self.tee("  --- APBS stdout:")
        self.tee(stdoutdata)
        self.tee("  --- APBS stderr:")
        self.tee(stderrdata)
      
      # Runs molsurf to calculate Connolly surface
      apolar_energy = np.inf
      p = subprocess.Popen([self._FNs['molsurf'], pqr_FN, '1.4'], \
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
      (stdoutdata, stderrdata) = p.communicate()
      p.wait()

      for line in stdoutdata.split('\n'):
        if line.startswith('surface area ='):
          apolar_energy = float(line.split('=')[-1]) * \
            0.0072 * MMTK.Units.kcal/MMTK.Units.mol

      if debug:
        molsurf_out_FN = moiety+'molsurf-mg-manual.out'
        molsurf_out_F = open(molsurf_out_FN, 'w')
        molsurf_out_F.write(stdoutdata)
        molsurf_out_F.close()
      else:
        for FN in [inpcrd_FN, pqr_FN, apbs_in_FN, 'io.mc']:
          os.remove(FN)
      
      E.append([polar_energy, apolar_energy, np.nan])

      if np.isinf(polar_energy) or np.isinf(apolar_energy):
        break

    os.chdir(self.dir['start'])
    if not debug:
      os.system('rm -rf '+apbs_dir)
    return np.array(E, dtype=float)*MMTK.Units.kJ/MMTK.Units.mol

  def _get_APBS_grid_spacing(self, RFILLRATIO=RFILLRATIO):
    factor = 1.0/MMTK.Units.Ang
    
    def roundUpDime(x):
      return (np.ceil((x.astype(float)-1)/32)*32+1).astype(int)

    self._set_universe_evaluator({'MM':True, 'T':self.T_HIGH, 'ELE':1})
    gd = self._forceFields['ELE'].grid_data
    focus_dims = roundUpDime(gd['counts'])
    focus_center = factor*(gd['counts']*gd['spacing']/2. + gd['origin'])
    focus_spacing = factor*gd['spacing'][0]

    min_xyz = np.array([min(factor*self.confs['receptor'][a,:]) for a in range(3)])
    max_xyz = np.array([max(factor*self.confs['receptor'][a,:]) for a in range(3)])
    mol_range = max_xyz - min_xyz
    mol_center = (min_xyz + max_xyz)/2.

    # The full grid spans RFILLRATIO times the range of the receptor
    # and the focus grid, whatever is larger
    full_spacing = 1.0
    full_min = np.minimum(mol_center - mol_range/2.*RFILLRATIO, \
                          focus_center - focus_dims*focus_spacing/2.*RFILLRATIO)
    full_max = np.maximum(mol_center + mol_range/2.*RFILLRATIO, \
                          focus_center + focus_dims*focus_spacing/2.*RFILLRATIO)
    full_dims = roundUpDime((full_max-full_min)/full_spacing)
    full_center = (full_min + full_max)/2.

    self._apbs_grid = {\
      'dime':[full_dims, focus_dims], \
      'gcent':[full_center, focus_center], \
      'spacing':[full_spacing, focus_spacing]}

  def _combine_MM_and_solvent(self, E, toParse=None):
    if toParse is None:
      toParse = [k for k in E.keys() \
        if (E[k] is not None) and (len(E[k].shape)==2)]
    for key in toParse:
      if np.isnan(E[key][:,-1]).all():
        E[key] = E[key][:,:-1]
        if key.find('sander')>-1:
          prefix = key.split('_')[0][:-6]
          for c in [0,1,2,6,7]:
            E[key][:,c] = E[prefix+'sander_Gas'][:,c]
        elif key.find('gbnsr6')>-1:
          prefix = key.split('_')[0][:-6]
          for (gbnsr6_ind, sander_ind) in [(0,0),(1,1),(2,2),(3,6),(5,3)]:
            E[key][:,gbnsr6_ind] = E[prefix+'sander_Gas'][:,sander_ind]
        elif key.find('APBS_PBSA'):
          prefix = key[:-9]
          totalMM = np.transpose(np.atleast_2d(E[prefix+'NAMD_Gas'][:,-1]))
          E[key] = np.hstack((E[key],totalMM))
        E[key] = np.hstack((E[key],np.sum(E[key],1)[...,None]))

  def get_rmsds(self, confs):
    import AlGDock.IO
    IO_dock6_mol2 = AlGDock.IO.dock6_mol2()

    ref_FN = os.path.abspath(os.path.join(self.dir['dock'],'rmsd_reference.mol2'))
    if not os.path.isfile(ref_FN):
      ref_conf = self.confs['rmsd'][self.molecule.prmtop_atom_order,:]*10.
      IO_dock6_mol2.write(self._FNs['score'], [ref_conf], ref_FN)
    target_FN = os.path.abspath(os.path.join(self.dir['dock'],'rmsd_target.mol2'))
    IO_dock6_mol2.write(self._FNs['score'], confs, target_FN)

    self._FNs['dock6'] = a.findPaths(['dock6'])['dock6']
    in_FN = os.path.abspath(os.path.join(self.dir['dock'],'rmsd.in'))
    in_F = open(in_FN,'w')
    in_F.write('''
ligand_atom_file                                             {1}
limit_max_ligands                                            no
skip_molecule                                                no
read_mol_solvation                                           no
calculate_rmsd                                               yes
use_rmsd_reference_mol                                       yes
rmsd_reference_filename                                      {2}
use_database_filter                                          no
orient_ligand                                                no
use_internal_energy                                          no
flexible_ligand                                              no
bump_filter                                                  no
score_molecules                                              no
atom_model                                                   all
vdw_defn_file                                                {0}/parameters/vdw_AMBER_parm99.defn
flex_defn_file                                               {0}/parameters/flex.defn
flex_drive_file                                              {0}/parameters/flex_drive.tbl
ligand_outfile_prefix                                        rmsd
write_orientations                                           no
num_scored_conformers                                        1000
write_conformations                                          no
cluster_conformations                                        no
rank_ligands                                                 no
'''.format(self._FNs['dock6'][:-10], target_FN, ref_FN))
    in_F.close()

    dir_o = os.getcwd()
    os.chdir(self.dir['dock'])
    import subprocess
    p = subprocess.Popen(\
      [self._FNs['dock6'], '-i',in_FN], \
      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (stdoutdata, stderrdata) = p.communicate()
    p.wait()
    os.chdir(dir_o)
    
    scored_FN = os.path.abspath(\
      os.path.join(self.dir['dock'],'rmsd_scored.mol2'))
    if not os.path.isfile(scored_FN):
      raise Exception('DOCK 6 failed to calculate rmsd')

    F = open(scored_FN,'r')
    rmsds = [float(line.split('\t')[-1]) for line in F.read().split('\n') \
      if line.startswith('########## HA_RMSDh:')]
    F.close()

    os.remove(target_FN)
    os.remove(in_FN)
    os.remove(scored_FN)
    if not ref_FN in self._toClear:
      self._toClear.append(ref_FN)
    
    return np.array(rmsds)/10.

  def _write_traj(self, traj_FN, confs, moiety, \
      title='', factor=1.0/MMTK.Units.Ang):
    """
    Writes a trajectory file
    """
    
    if traj_FN is None:
      return
    if traj_FN.endswith('.pqr'):
      return
    if traj_FN.endswith('.crd'):
      return
    if os.path.isfile(traj_FN):
      return
    
    traj_dir = os.path.dirname(os.path.abspath(traj_FN))
    if not os.path.isdir(traj_dir):
      os.system('mkdir -p '+traj_dir)

    import AlGDock.IO
    if traj_FN.endswith('.dcd'):
      IO_dcd = AlGDock.IO.dcd(self.molecule,
        ligand_atom_order = self.molecule.prmtop_atom_order, \
        receptorConf = self.confs['receptor'], \
        ligand_first_atom = self._ligand_first_atom)
      IO_dcd.write(traj_FN, confs,
        includeReceptor=(moiety.find('R')>-1),
        includeLigand=(moiety.find('L')>-1))
    elif traj_FN.endswith('.mdcrd'):
      if (moiety.find('R')>-1):
        receptor_0 = factor*self.confs['receptor'][:self._ligand_first_atom,:]
        receptor_1 = factor*self.confs['receptor'][self._ligand_first_atom:,:]

      if not isinstance(confs,list):
        confs = [confs]
      if (moiety.find('R')>-1):
        if (moiety.find('L')>-1):
          confs = [np.vstack((receptor_0, \
            conf[self.molecule.prmtop_atom_order,:]/MMTK.Units.Ang, \
            receptor_1)) for conf in confs]
        else:
          confs = [factor*self.confs['receptor']]
      else:
        confs = [conf[self.molecule.prmtop_atom_order,:]/MMTK.Units.Ang \
          for conf in confs]
      
      import AlGDock.IO
      IO_crd = AlGDock.IO.crd()
      IO_crd.write(traj_FN, confs, title, trajectory=True)
      self.tee("  wrote %d configurations to %s"%(len(confs), traj_FN))
    else:
      raise Exception('Unknown trajectory type')

  def _load_pkl_gz(self, FN):
    if os.path.isfile(FN) and os.path.getsize(FN)>0:
      F = gzip.open(FN,'r')
      try:
        data = pickle.load(F)
      except:
        self.tee('  error loading '+FN)
        F.close()
        return None
      F.close()
      return data
    else:
      return None

  def _write_pkl_gz(self, FN, data, quiet=False):
    F = gzip.open(FN,'w')
    pickle.dump(data,F)
    F.close()
    if not quiet:
      self.tee("  wrote to "+os.path.basename(FN))

  def _load(self, p, pose):
    if p=='dock' and pose>-1:
      progress_FN = os.path.join(self.dir[p],'%s_progress_pose%03d.pkl.gz'%(p, pose))
      data_FN = os.path.join(self.dir[p],'%s_data_pose%03d.pkl.gz'%(p, pose))
    else:
      progress_FN = os.path.join(self.dir[p],'%s_progress.pkl.gz'%(p))
      data_FN = os.path.join(self.dir[p],'%s_data.pkl.gz'%(p))

    saved = {'progress':self._load_pkl_gz(progress_FN),
             'data':self._load_pkl_gz(data_FN)}
    if (saved['progress'] is None) or (saved['data'] is None):
      if os.path.isfile(progress_FN):
        os.remove(progress_FN)
      if os.path.isfile(data_FN):
        os.remove(data_FN)
      if p=='dock' and pose>-1:
        progress_FN = os.path.join(self.dir[p],'%s_progress_pose%03d.pkl.gz.BAK'%(p, pose))
        data_FN = os.path.join(self.dir[p],'%s_data_pose%03d.pkl.gz.BAK'%(p, pose))
      else:
        progress_FN = os.path.join(self.dir[p],'%s_progress.pkl.gz.BAK'%(p))
        data_FN = os.path.join(self.dir[p],'%s_data.pkl.gz.BAK'%(p))

      saved = {'progress':self._load_pkl_gz(progress_FN),
               'data':self._load_pkl_gz(data_FN)}
      if (saved['progress'] is None):
        print '  no progress information for %s'%p
      elif (saved['data'] is None):
        saved['progress'] = None
        print '  missing data in %s'%p
      else:
        print '  using stored progress and data in %s'%p
    self._clear(p)
    
    params = None
    if saved['progress'] is not None:
      params = saved['progress'][0]
      setattr(self,'%s_protocol'%p,saved['progress'][1])
      setattr(self,'_%s_cycle'%p,saved['progress'][2])
    if saved['data'] is not None:
      if p=='dock' and saved['data'][0] is not None:
        (self._n_trans, self._max_n_trans, self._random_trans, \
         self._n_rot, self._max_n_rot, self._random_rotT) = saved['data'][0]
      # New file format (after 6/13/2016) storing starting poses
      self.confs[p]['starting_poses'] = saved['data'][1]
      self.confs[p]['replicas'] = saved['data'][2]
      self.confs[p]['seeds'] = saved['data'][3]
      self.confs[p]['SmartDarting'] = saved['data'][4]
      self.confs[p]['samples'] = saved['data'][5]
      setattr(self,'%s_Es'%p, saved['data'][6])
      if saved['data'][5] is not None:
        cycle = len(saved['data'][5][-1])
        setattr(self,'_%s_cycle'%p,cycle)
      else:
        setattr(self,'_%s_cycle'%p,0)
    if getattr(self,'%s_protocol'%p)==[] or \
        (not getattr(self,'%s_protocol'%p)[-1]['crossed']):
      setattr(self,'_%s_cycle'%p,0)
    return params

  def _clear(self, p):
    setattr(self,'%s_protocol'%p,[])
    setattr(self,'_%s_cycle'%p,0)
    self.confs[p]['starting_poses'] = None
    self.confs[p]['replicas'] = None
    self.confs[p]['seeds'] = None
    self.confs[p]['SmartDarting'] = []
    self.confs[p]['samples'] = None
    setattr(self,'%s_Es'%p,None)
  
  def _clear_f_RL(self):
    # stats_RL will include internal energies, interaction energies,
    # the cycle by which the bound state is equilibrated,
    # the mean acceptance probability between replica exchange neighbors,
    # and the rmsd, if applicable
    phase_f_RL_keys = \
      [phase+'_solv' for phase in self.params['dock']['phases']]

    # Initialize variables as empty lists
    stats_RL = [('u_K_'+FF,[]) \
      for FF in ['ligand','sampled']+self.params['dock']['phases']]
    stats_RL += [('Psi_'+FF,[]) \
      for FF in ['grid']+self.params['dock']['phases']]
    stats_RL += [(item,[]) \
      for item in ['equilibrated_cycle','cum_Nclusters','mean_acc','rmsd']]
    self.stats_RL = dict(stats_RL)
    self.stats_RL['protocol'] = self.dock_protocol
    # Free energy components
    self.f_RL = dict([(key,[]) \
      for key in ['grid_MBAR'] + phase_f_RL_keys])
    # Binding PMF estimates
    self.B = {'MMTK_MBAR':[]}
    for phase in self.params['dock']['phases']:
      for method in ['min_Psi','mean_Psi','EXP','MBAR']:
        self.B[phase+'_'+method] = []

    # Store empty list
    if self.params['dock']['pose']==-1:
      f_RL_FN = os.path.join(self.dir['dock'],'f_RL.pkl.gz')
    else:
      f_RL_FN = os.path.join(self.dir['dock'], \
        'f_RL_pose%03d.pkl.gz'%self.params['dock']['pose'])
    if hasattr(self,'run_type') and (not self._run_type=='timed'):
      self._write_pkl_gz(f_RL_FN, (self.f_L, self.stats_RL, self.f_RL, self.B))

  def _save(self, p, keys=['progress','data']):
    """
    Saves the protocol, 
    cycle counts,
    random orientation parameters (for docking),
    replica configurations,
    sampled configurations,
    and energies
    """
    random_orient = None
    if p=='dock' and hasattr(self,'_n_trans'):
        random_orient = (self._n_trans, self._max_n_trans, self._random_trans, \
           self._n_rot, self._max_n_rot, self._random_rotT)
  
    arg_dict = dict([tp for tp in self.params[p].items() \
                      if not tp[0] in ['repX_cycles']])
    if p=='cool':
      fn_dict = convert_dictionary_relpath({
          'ligand_database':self._FNs['ligand_database'],
          'forcefield':self._FNs['forcefield'],
          'frcmodList':self._FNs['frcmodList'],
          'tarball':{'L':self._FNs['tarball']['L']},
          'prmtop':{'L':self._FNs['prmtop']['L']},
          'inpcrd':{'L':self._FNs['inpcrd']['L']}},
          relpath_o=None, relpath_n=self.dir['cool'])
    elif p=='dock':
      fn_dict = convert_dictionary_relpath(
          dict(self._FNs.items()), relpath_o=None, relpath_n=self.dir['dock'])
    params = (fn_dict,arg_dict)
    
    saved = {
      'progress': (params,
                   getattr(self,'%s_protocol'%p),
                   getattr(self,'_%s_cycle'%p)),
      'data': (random_orient,
               self.confs[p]['starting_poses'],
               self.confs[p]['replicas'],
               self.confs[p]['seeds'],
               self.confs[p]['SmartDarting'],
               self.confs[p]['samples'],
               getattr(self,'%s_Es'%p))}
    
    for key in keys:
      if p=='dock' and self.params['dock']['pose']>-1:
        saved_FN = os.path.join(self.dir[p],'%s_%s_pose%03d.pkl.gz'%(\
          p, key, self.params['dock']['pose']))
      else:
        saved_FN = os.path.join(self.dir[p],'%s_%s.pkl.gz'%(p,key))
      if not os.path.isdir(self.dir[p]):
        os.system('mkdir -p '+self.dir[p])
      if os.path.isfile(saved_FN):
        os.rename(saved_FN,saved_FN+'.BAK')
      self._write_pkl_gz(saved_FN, saved[key], quiet=True)
    self.tee('  saved %s progress and data'%p)

  def _set_lock(self, p):
    if not os.path.isdir(self.dir[p]):
      os.system('mkdir -p '+self.dir[p])
    if p=='dock' and self.params['dock']['pose']>-1:
      lockFN = os.path.join(self.dir[p], \
        '.lock_pose%03d'%self.params['dock']['pose'])
    else:
      lockFN = os.path.join(self.dir[p],'.lock')
    if os.path.isfile(lockFN):
      raise Exception(p + ' is locked')
    else:
      lockF = open(lockFN,'w')
      lockF.close()
    if p=='dock' and self.params['dock']['pose']>-1:
      logFN = os.path.join(self.dir[p],'%s_pose%03d_log.txt'%(\
        p, self.params['dock']['pose']))
    else:
      logFN = os.path.join(self.dir[p],p+'_log.txt')
    self.log = open(logFN,'a')

  def _clear_lock(self, p):
    if p=='dock' and self.params['dock']['pose']>-1:
      lockFN = os.path.join(self.dir[p], \
        '.lock_pose%03d'%self.params['dock']['pose'])
    else:
      lockFN = os.path.join(self.dir[p],'.lock')
    if os.path.isfile(lockFN):
      os.remove(lockFN)
    if hasattr(self,'log'):
      self.log.close()
      del self.log

  def tee(self, var, process=None):
    print var
    if hasattr(self,'log'):
      if isinstance(var,str):
        self.log.write(var+'\n')
      else:
        self.log.write(repr(var)+'\n')
      self.log.flush()
    elif process is not None:
      self._set_lock(process)
      if isinstance(var,str):
        self.log.write(var+'\n')
      else:
        self.log.write(repr(var)+'\n')
      self.log.flush()
      self._clear_lock(process)

  def __del__(self):
    for p in ['cool', 'dock']:
      if self.params[p]['sampler'] == 'MixedHMC':
        self.sampler[p].TDintegrator.Clear()
    if (not DEBUG) and len(self._toClear)>0:
      print "\n>>> Clearing files"
      for FN in self._toClear:
        if os.path.isfile(FN):
          os.remove(FN)
          print '  removed '+os.path.relpath(FN,self.dir['start'])

if __name__ == '__main__':
  import argparse
  parser = argparse.ArgumentParser(
    description='Molecular docking with adaptively scaled alchemical interaction grids')
  
  for key in arguments.keys():
    parser.add_argument('--'+key, **arguments[key])
  args = parser.parse_args()

  if args.run_type in ['render_docked', 'render_intermediates']:
    from AlGDock.BindingPMF_plots import BPMF_plots
    self = BPMF_plots(**vars(args))
  else:
    self = BPMF(**vars(args))
