#!/usr/bin/env python

"""
This module implements specific error handlers for VASP runs. These handlers
tries to detect common errors in vasp runs and attempt to fix them on the fly
by modifying the input files.
"""

from __future__ import division

__author__ = "Shyue Ping Ong"
__version__ = "0.1"
__maintainer__ = "Shyue Ping Ong"
__email__ = "shyuep@gmail.com"
__status__ = "Beta"
__date__ = "2/4/13"

import os
import logging
import tarfile
import time
import glob
import operator

from custodian.custodian import ErrorHandler
from pymatgen.io.vaspio.vasp_input import Poscar, VaspInput
from pymatgen.transformations.standard_transformations import \
    PerturbStructureTransformation, SupercellTransformation
from pymatgen.serializers.json_coders import MSONable

from pymatgen.io.vaspio.vasp_output import Vasprun, Oszicar
from custodian.ansible.intepreter import Modder
from custodian.ansible.actions import FileActions, DictActions


class VaspErrorHandler(ErrorHandler, MSONable):

    error_msgs = {
        "tet": ["Tetrahedron method fails for NKPT<4",
                "Fatal error detecting k-mesh",
                "Fatal error: unable to match k-point",
                "Routine TETIRR needs special values"],
        "inv_rot_mat": ["inverse of rotation matrix was not found (increase "
                        "SYMPREC)"],
        "brmix": ["BRMIX: very serious problems"],
        "subspacematrix": ["WARNING: Sub-Space-Matrix is not hermitian in "
                           "DAV"],
        "tetirr": ["Routine TETIRR needs special values"],
        "incorrect_shift": ["Could not get correct shifts"],
        "real_optlay": ["REAL_OPTLAY: internal error"],
        "rspher": ["ERROR RSPHER"],
        "dentet": ["DENTET"],
        "too_few_bands": ["TOO FEW BANDS"],
        "triple_product": ["ERROR: the triple product of the basis vectors"],
        "rot_matrix": ["Found some non-integer element in rotation matrix"],
        "brions": ["BRIONS problems: POTIM should be increased"]
    }

    def __init__(self, output_filename="vasp.out"):
        self.output_filename = output_filename

    def check(self):
        self.errors = set()
        with open(self.output_filename, "r") as f:
            for line in f:
                l = line.strip()
                for err, msgs in VaspErrorHandler.error_msgs.items():
                    for msg in msgs:
                        if l.find(msg) != -1:
                            self.errors.add(err)
        return len(self.errors) > 0

    def correct(self):
        backup()
        actions = []
        vi = VaspInput.from_directory(".")

        if "tet" in self.errors or "dentet" in self.errors:
            actions.append({"dict": "INCAR",
                            "action": {"_set": {"ISMEAR": 0}}})
        if "inv_rot_mat" in self.errors:
            actions.append({"dict": "INCAR",
                            "action": {"_set": {"SYMPREC": 1e-8}}})
        if "brmix" in self.errors:
            actions.append({"dict": "INCAR",
                            "action": {"_set": {"IMIX": 1}}})
        if "subspacematrix" in self.errors or "rspher" in self.errors or \
                "real_optlay" in self.errors:
            actions.append({"dict": "INCAR",
                            "action": {"_set": {"LREAL": False}}})
        if "tetirr" in self.errors or "incorrect_shift" in self.errors:
            actions.append({"dict": "KPOINTS",
                            "action": {"_set": {"generation_style": "Gamma"}}})
        if "too_few_bands" in self.errors:
            if "NBANDS" in vi["INCAR"]:
                nbands = int(vi["INCAR"]["NBANDS"])
            else:
                with open("OUTCAR", "r") as f:
                    for line in f:
                        if "NBANDS" in line:
                            try:
                                d = line.split("=")
                                nbands = int(d[-1].strip())
                                break
                            except:
                                pass
            actions.append({"dict": "INCAR",
                            "action": {"_set": {"NBANDS": int(1.2 * nbands)}}})

        if "triple_product" in self.errors:
            s = vi["POSCAR"].structure
            trans = SupercellTransformation(((1, 0, 0), (0, 0, 1), (0, 1, 0)))
            new_s = trans.apply_transformation(s)
            actions.append({"dict": "POSCAR",
                            "action": {"_set": {"structure": new_s.to_dict}},
                            "transformation": trans.to_dict})

        if "rot_matrix" in self.errors:
            s = vi["POSCAR"].structure
            trans = PerturbStructureTransformation(0.05)
            new_s = trans.apply_transformation(s)
            actions.append({"dict": "POSCAR",
                            "action": {"_set": {"structure": new_s.to_dict}},
                            "transformation": trans.to_dict})
        if "brions" in self.errors:
            potim = vi["INCAR"].get("POTIM", 0.5) + 0.1
            actions.append({"dict": "INCAR",
                            "action": {"_set": {"POTIM": potim}}})
        m = Modder()
        modified = []
        for a in actions:
            modified.append(a["dict"])
            vi[a["dict"]] = m.modify_object(a["action"], vi[a["dict"]])
        for f in modified:
            vi[f].write_file(f)
        return {"errors": list(self.errors), "actions": actions}

    @property
    def is_monitor(self):
        return True

    def __str__(self):
        return "VaspErrorHandler"

    @property
    def to_dict(self):
        return {"@module": self.__class__.__module__,
                "@class": self.__class__.__name__,
                "output_filename": self.output_filename}

    @staticmethod
    def from_dict(d):
        return VaspErrorHandler(d["output_filename"])


class MeshSymmetryErrorHandler(ErrorHandler, MSONable):
    """
    Corrects the mesh symmetry error in VASP. This error is sometimes
    non-fatal. So this error handler only checks at the end of the run,
    and if the run has converged, no error is recorded.
    """

    def __init__(self, output_filename="vasp.out", 
                 output_vasprun="vasprun.xml"):
        self.output_filename = output_filename
        self.output_vasprun = output_vasprun

    def check(self):
        msg = "Reciprocal lattice and k-lattice belong to different class of" \
              " lattices."
        try:
            v = Vasprun(self.output_vasprun)
            if v.converged:
                return False
        except:
            pass
        with open(self.output_filename, "r") as f:
            for line in f:
                l = line.strip()
                if l.find(msg) != -1:
                    return True
        return False

    def correct(self):
        backup()
        vi = VaspInput.from_directory(".")
        m = reduce(operator.mul, vi["KPOINTS"].kpts[0])
        m = max(int(round(m ** (1 / 3))), 1)
        if vi["KPOINTS"].style.lower().startswith("m"):
            m += m % 2
        actions = [{"dict": "KPOINTS",
                    "action": {"_set": {"kpoints": [[m] * 3]}}}]
        m = Modder()
        modified = []
        for a in actions:
            modified.append(a["dict"])
            vi[a["dict"]] = m.modify_object(a["action"], vi[a["dict"]])
        for f in modified:
            vi[f].write_file(f)
        return {"errors": ["mesh_symmetry"], "actions": actions}

    @property
    def is_monitor(self):
        return False

    def __str__(self):
        return "MeshSymmetryErrorHandler"

    @property
    def to_dict(self):
        return {"@module": self.__class__.__module__,
                "@class": self.__class__.__name__,
                "output_filename": self.output_filename}

    @staticmethod
    def from_dict(d):
        return MeshSymmetryErrorHandler(d["output_filename"])


class UnconvergedErrorHandler(ErrorHandler, MSONable):
    """
    Check if a run is converged. Switches to ALGO = Normal.
    """
    def __init__(self, output_filename="vasprun.xml"):
        self.output_filename = output_filename

    def check(self):
        try:
            v = Vasprun(self.output_filename)
            if not v.converged:
                return True
        except:
            pass
        return False

    def correct(self):
        backup()
        actions = [{"file": "CONTCAR",
                    "action": {"_file_copy": {"dest": "POSCAR"}}},
                   {"dict": "INCAR",
                    "action": {"_set": {"ISTART": 1,
                                        "ALGO": "Normal",
                                        "NELMDL": 6,
                                        "BMIX": 0.001,
                                        "AMIX_MAG": 0.8,
                                        "BMIX_MAG": 0.001}}}]
        vi = VaspInput.from_directory(".")
        m = Modder(actions=[DictActions, FileActions])
        for a in actions:
            if "dict" in a:
                vi[a["dict"]] = m.modify_object(a["action"], vi[a["dict"]])
            elif "file" in a:
                m.modify(a["action"], a["file"])
        vi["INCAR"].write_file("INCAR")

        return {"errors": ["Unconverged"], "actions": actions}

    def __str__(self):
        return "Run unconverged."

    @property
    def is_monitor(self):
        return False

    @property
    def to_dict(self):
        return {"@module": self.__class__.__module__,
                "@class": self.__class__.__name__,
                "output_filename": self.output_filename}

    @staticmethod
    def from_dict(d):
        return UnconvergedErrorHandler(d["output_filename"])


class FrozenJobErrorHandler(ErrorHandler):

    def __init__(self, output_filename="vasp.out", timeout=3600):
        """
        Detects an error when the output file has not been updated
        in timeout seconds. Perturbs structure and restarts
        """
        self.output_filename = output_filename
        self.timeout = timeout

    def check(self):
        st = os.stat(self.output_filename)
        if time.time() - st.st_mtime > self.timeout:
            return True

    def correct(self):
        backup()
        p = Poscar.from_file("POSCAR")
        s = p.structure
        trans = PerturbStructureTransformation(0.05)
        new_s = trans.apply_transformation(s)
        actions = [{"dict": "POSCAR",
                    "action": {"_set": {"structure": new_s.to_dict}},
                    "transformation": trans.to_dict}]
        m = Modder()
        vi = VaspInput.from_directory(".")
        for a in actions:
            vi[a["dict"]] = m.modify_object(a["action"], vi[a["dict"]])
        vi["POSCAR"].write_file("POSCAR")

        return {"errors": ["Frozen job"], "actions": actions}

    @property
    def is_monitor(self):
        return True

    @property
    def to_dict(self):
        return {"@module": self.__class__.__module__,
                "@class": self.__class__.__name__,
                "output_filename": self.output_filename,
                "timeout": self.timeout}

    @staticmethod
    def from_dict(d):
        return FrozenJobErrorHandler(d["output_filename"],
                                     timeout=d["timeout"])


class NonConvergingErrorHandler(ErrorHandler, MSONable):
    """
    Check if a run is hitting the maximum number of electronic steps at the
    last 10 ionic steps. If so, kill the job.
    """
    def __init__(self, output_filename="OSZICAR"):
        self.output_filename = output_filename

    def check(self):
        vi = VaspInput.from_directory(".")
        nelm = vi["INCAR"].get("NELM", 60)
        oszicar = Oszicar(self.output_filename)
        esteps = oszicar.ionic_steps
        if len(esteps) > 10:
            return all([len(e) == nelm for e in esteps[-11:-1]])
        return False

    def correct(self):
        #Unfixable error. Just return None for actions.
        return {"errors": ["Non-converging job"], "actions": None}

    def __str__(self):
        return "Run not converging."

    @property
    def is_monitor(self):
        return True

    @property
    def to_dict(self):
        return {"@module": self.__class__.__module__,
                "@class": self.__class__.__name__,
                "output_filename": self.output_filename}

    @staticmethod
    def from_dict(d):
        return NonConvergingErrorHandler(d["output_filename"])


def backup():
    error_num = max([0] + [int(f.split(".")[1])
                           for f in glob.glob("error.*.tar.gz")])
    filename = "error.{}.tar.gz".format(error_num + 1)
    logging.info("Backing up run to {}.".format(filename))
    tar = tarfile.open(filename, "w:gz")
    for f in os.listdir("."):
        if not (f.startswith("error") and f.endswith(".tar.gz")):
            tar.add(f)
    tar.close()
