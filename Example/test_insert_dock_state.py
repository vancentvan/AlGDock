# The example is 1of6

import AlGDock.BindingPMF_plots
import os, shutil, glob

phases = ['NAMD_Gas', 'NAMD_OBC']

self = AlGDock.BindingPMF_plots.BPMF_plots(\
  dir_dock='dock', dir_cool='cool',\
  ligand_tarball='prmtopcrd/ligand.tar.gz', \
  ligand_database='ligand.db', \
  forcefield='prmtopcrd/gaff.dat', \
  ligand_prmtop='ligand.prmtop', \
  ligand_inpcrd='ligand.trans.inpcrd', \
  ligand_mol2='ligand.mol2', \
  ligand_rb='ligand.rb', \
  receptor_tarball='prmtopcrd/receptor.tar.gz', \
  receptor_prmtop='receptor.prmtop', \
  receptor_inpcrd='receptor.trans.inpcrd', \
  receptor_fixed_atoms='receptor.pdb', \
  complex_tarball='prmtopcrd/complex.tar.gz', \
  complex_prmtop='complex.prmtop', \
  complex_inpcrd='complex.trans.inpcrd', \
  complex_fixed_atoms='complex.pdb', \
  score = 'prmtopcrd/anchor_and_grow_scored.mol2', \
  pose=-1, \
  rmsd=True, \
  dir_grid='grids', \
  protocol='Adaptive', cool_therm_speed=0.5, dock_therm_speed=0.5, \
  sampler='HMC', fraction_TD=0.5, TD_steps_per_trial=10, \
  MCMC_moves=1, \
  seeds_per_state=10, steps_per_seed=200, darts_per_seed=0, \
  sweeps_per_cycle=25, attempts_per_sweep=100, \
  steps_per_sweep=100, darts_per_sweep=0, \
  cool_repX_cycles=3, dock_repX_cycles=4, \
  site='Sphere', site_center=[1.74395, 1.74395, 1.74395], \
  site_max_R=0.6, \
  site_density=10., \
  phases=phases, \
  cores=-1, \
  random_seed=-1)
self._run('all')

self._insert_dock_state(0.95)
self.calc_f_RL()