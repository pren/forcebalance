import unittest
import os, sys
import tarfile
from __init__ import ForceBalanceTestCase, TestValues
from forcebalance.parser import parse_inputs
from forcebalance.forcefield import FF
from forcebalance.objective import Objective
from forcebalance.optimizer import Optimizer, Counter
from collections import OrderedDict
from numpy import array

class TestWaterTutorial(ForceBalanceTestCase):
    def setUp(self):
        super(ForceBalanceTestCase,self).setUp()
        os.chdir('studies/001_water_tutorial')
        targets = tarfile.open('targets.tar.bz2','r')
        targets.extractall()
        targets.close()

    def tearDown(self):
        os.system('rm -rf results targets backups temp')
        super(ForceBalanceTestCase,self).tearDown()

    def runTest(self):
        """Check tutorial runs without errors"""
        input_file='very_simple.in'

        ## The general options and target options that come from parsing the input file
        options, tgt_opts = parse_inputs(input_file)

        self.assertEqual(dict,type(options), msg="\nParser gave incorrect type for options")
        self.assertEqual(list,type(tgt_opts), msg="\nParser gave incorrect type for tgt_opts")
        for target in tgt_opts:
            self.assertEqual(dict, type(target), msg="\nParser gave incorrect type for target dict")

        ## The force field component of the project
        forcefield  = FF(options)
        self.assertEqual(FF, type(forcefield), msg="\nExpected forcebalance forcefield object")

        ## The objective function
        objective   = Objective(options, tgt_opts, forcefield)
        self.assertEqual(Objective, type(objective), msg="\nExpected forcebalance objective object")

        ## The optimizer component of the project
        optimizer   = Optimizer(options, objective, forcefield)
        self.assertEqual(Optimizer, type(optimizer), msg="\nExpected forcebalance optimizer object")

        ## Actually run the optimizer.
        result = optimizer.Run()
        # expected results taken from previous runs. Update this if it changes and seems reasonable

        expected = array((0.03316133, 0.04331116, 0.00550698, -0.04593296, 0.01549711, -0.37654517, 0.00249289, 0.01187446, 0.1510803))
        self.assertEqual(expected,result,msg="\nCalculation result differs from previous value")

        # Tutorial calculation converges in 5 iterations
        self.assertGreaterEqual(5, Counter(), msg="\nCalculation took longer than expected to converge")
        

if __name__ == '__main__':
    unittest.main()