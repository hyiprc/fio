__all__ = ['Box']

import re
import sys
from types import MappingProxyType

import numpy as np

from . import ERROR, logger


def _flatten(alist: list):
    """generator, flatten a deeply nested list"""
    try:
        for item in alist:
            if isinstance(item, list):
                for subitem in flatten(item):
                    yield subitem
            else:
                yield item
    except TypeError:
        yield alist


def flatten(alist: list):
    """flatten a deeply nested list"""
    return list(_flatten(alist))


def normalize(a, order=2, axis=-1):
    """Normalize row-listed vectors of a"""
    l2 = np.atleast_1d(np.linalg.norm(a, order, axis))
    l2[l2 == 0] = 1.0
    return np.atleast_1d(np.squeeze(a / np.expand_dims(l2, axis)))


def cross(v, u):
    """Cross products of v and u (match row-by-row)"""
    return normalize(np.cross(v, u))


deg2rad = np.pi / 180.0
rad2deg = 180.0 / np.pi
abg = ('alpha', 'beta', 'gamma')  # angle between b c, a c, a b


class BoxInputDict(dict):
    """This is a non-mutable dict"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__dict__ = self

    def __setattr__(self, key, value):
        if key not in [*self.keys(), '__dict__']:
            raise KeyError(f"'{key}' is not a Box input parameter")
        else:
            super().__setattr__(key, value)
            self._check_allow_tilt(key, value)

    def __setitem__(self, key, value):
        if key not in self:
            raise KeyError(f"'{key}' is not a Box input parameter")
        else:
            super().__setitem__(key, value)
            self._check_allow_tilt(key, value)

    def update(self, *args, **kwargs):
        frozen = set(self.keys())
        super().update(*args, **kwargs)
        invalidkeys = set(self.keys()).difference(frozen)
        if invalidkeys:
            logger.warning(
                f"Ignored invalid Box input parameters: {invalidkeys}"
            )
            for k in invalidkeys:
                self.pop(k)

    def _check_allow_tilt(self, key, value):
        if key in abg and value != 90:
            self.__dict__['allow_tilt'] = True
        elif key == 'allow_tilt' and not self['allow_tilt']:
            if any([self[k] != 90 for k in abg]):
                logger.warning(f"Non-orthogonal box, keeping {key} = True")
                self.__dict__['allow_tilt'] = True


class Box:
    def __init__(self, inputdict: dict = {}):
        self.input = BoxInputDict(
            {
                'x0': 0.0,
                'y0': 0.0,
                'z0': 0.0,
                'lx': 1.0,
                'ly': 1.0,
                'lz': 1.0,
                'alpha': 90.0,
                'beta': 90.0,
                'gamma': 90.0,
                'allow_tilt': False,
                'bx': 'pp',
                'by': 'pp',
                'bz': 'pp',
            }
        )
        self.input.update(inputdict)

        self.alias = {
            'vmd': 'lattice',
            'poscar': 'basis',
            'vasp': 'basis',
        }

    def __repr__(self):
        return f"<Box object at {hex(id(self))}, {self.input}>"

    def __str__(self):
        return self.report()

    def __getitem__(self, key):
        return self.input.get(key, self.output[key])

    def __setitem__(self, key, value):
        self.input[key] = value

    # -----------------------------------------------

    @property
    def output(self):
        _ = self.input.copy()

        for s in 'xyz':
            _[f'{s}lo'] = _[f'{s}0']
            _[f'{s}hi'] = _[f'{s}0'] + _[f'l{s}']

        lx, ly, lz = _['lx'], _['ly'], _['lz']

        _['allow_tilt'] |= any([_[s] != 90 for s in abg])

        _['cos_alpha'] = ca = np.cos(_['alpha'] * deg2rad)
        _['cos_beta'] = cb = np.cos(_['beta'] * deg2rad)
        _['cos_gamma'] = cg = np.cos(_['gamma'] * deg2rad)

        _['a'] = lx
        _['b'] = b = ly / (1.0 - cg**2.0) ** 0.5
        _['c'] = c = (
            lz
            / (1 - cb**2.0 - (ca - cg * cb) ** 2.0 / (1 - cg**2.0)) ** 0.5
        )

        _['xy'] = xy = b * cg
        _['xz'] = xz = c * cb
        _['yz'] = yz = (b * c * ca - xy * xz) / ly

        _['v'] = np.array(
            [
                [lx, 0.0, 0.0],  # v_a
                [xy, ly, 0.0],  # v_b
                [xz, yz, lz],  # v_c
            ]
        )

        # useful for coordinate transform
        _['u'] = normalize(_['v'])
        # useful for undo coordinate transform
        _['u_inv'] = np.linalg.inv(_['u'])

        # face normal, useful for cartesian to crystal fractional
        _['bn'] = np.r_[
            cross(_['u'][1], _['u'][2]),
            cross(_['u'][2], _['u'][0]),
            cross(_['u'][0], _['u'][1]),
        ].reshape(-1, 3)

        # get rid of small zero
        p = 9  # number < 1E-p is 0
        for s in _:
            try:
                _[s] = np.round(_[s], p)
            except Exception:
                continue

        return MappingProxyType(_)

    # -----------------------------------------------

    def _format_input(self, argv: str):
        if isinstance(argv, str):
            argv = re.split(r'\s*,\s*|\s+', argv.strip())
        # check input length
        if len(argv) == 1:
            ERROR("Read Box from file is not yet implemented")  # TODO
        else:
            return np.array(argv, dtype=float)

    def _guess_type(self, argv):
        if argv is None:
            raise ERROR('Missing Box input parameters', trace=0)
        elif len(argv) == 9:
            if argv[0] < argv[1] and argv[2] < argv[3] and argv[4] < argv[5]:
                return 'lmpdata'
            elif argv[0] < argv[1] and argv[3] < argv[4] and argv[6] < argv[7]:
                return 'lmpdump'
            else:
                return 'basis'
        elif len(argv) == 6:
            if argv[1] <= 1 and argv[3] <= 1 and argv[4] <= 1:
                return 'dcd'
            else:
                return 'lattice'
        else:
            raise ERROR('Incorrect Box input parameters', trace=0)

    def set_input(self, argv, typ=None):
        data = self._format_input(argv)
        # detect type
        typ = self.alias.get(typ, typ)
        if typ is None:
            typ = self._guess_type(data)
        # handle specific box type
        _ = self.input
        func = getattr(self, f'_input_{typ}')
        _.update(func(data))
        # always allow tilt if not orthogonal
        if any([_[s] != 90 for s in abg]):
            _['allow_tilt'] = True
        return typ

    def _input_basis(self, v: np.ndarray):
        """Basis Vectors:
        | v_a[0], v_a[1], v_a[2] |
        | v_b[0], v_b[1], v_b[2] |
        | v_c[0], v_c[1], v_c[2] |
        """
        v = np.array(v).reshape(3, 3)
        u = normalize(v)
        return {
            'lx': v[0, 0],
            'ly': v[1, 1],
            'lz': v[2, 2],
            'alpha': np.arccos(np.dot(u[1], u[2])) * rad2deg,
            'beta': np.arccos(np.dot(u[0], u[2])) * rad2deg,
            'gamma': np.arccos(np.dot(u[0], u[1])) * rad2deg,
        }

    def _input_lmpdata(self, v: np.ndarray):
        """LMPDATA: xlo, xhi, ylo, yhi, zlo, zhi, xy, xz, yz"""
        xlo, xhi, ylo, yhi, zlo, zhi, xy, xz, yz = v
        return {
            'x0': xlo,
            'y0': ylo,
            'z0': zlo,
            # fmt: off
            **self._input_basis([
                [xhi - xlo,          0,          0],  # noqa: E201, E241
                [       xy,  yhi - ylo,          0],  # noqa: E201, E241
                [       xz,         yz,  zhi - zlo],  # noqa: E201, E241
            ]),
            # fmt: on
        }

    def _input_lmpdump(self, v: np.ndarray):
        """LMPDUMP: xlo, xhi, xy, ylo, yhi, xz, zlo, zhi, yz"""
        return self._input_lmpdata(np.take(v, [0, 1, 3, 4, 6, 7, 2, 5, 8]))

    def _input_dcd(self, v: np.ndarray):
        """DCD: a, cos_gamma, b, cos_beta, cos_alpha, c"""
        a, cg, b, cb, ca, c = v
        ly = b * (1 - cg**2.0) ** 0.5
        lz = c * (1 - cb**2 - (ca - cg * cb) ** 2 / (1 - cg**2.0)) ** 0.5
        return {
            'lx': a,
            'ly': ly,
            'lz': lz,
            'alpha': np.arccos(ca) * rad2deg,
            'beta': np.arccos(cb) * rad2deg,
            'gamma': np.arccos(cg) * rad2deg,
        }

    def _input_lattice(self, v: np.ndarray):
        """Lattice Parameters: a, b, c, alpha, beta, gamma"""
        a, b, c, alpha, beta, gamma = v
        ca = np.cos(alpha * deg2rad)
        cb = np.cos(beta * deg2rad)
        cg = np.cos(gamma * deg2rad)
        return self._input_dcd([a, cg, b, cb, ca, c])

    # -----------------------------------------------

    def report(self, typ='all'):
        _ = self.output

        v = _['v']
        fmt_basis = (
            f" {v[0, 0]:15.9f}  {v[0, 1]:15.9f}  {v[0, 2]:15.9f}\n"
            f" {v[1, 0]:15.9f}  {v[1, 1]:15.9f}  {v[1, 2]:15.9f}\n"
            f" {v[2, 0]:15.9f}  {v[2, 1]:15.9f}  {v[2, 2]:15.9f}"
        )

        fmt_lattice = f"{_['a']:g} {_['b']:g} {_['c']:g} {_['alpha']:g} {_['beta']:g} {_['gamma']:g}  a b c alpha beta gamma"

        fmt_lmpdata = (
            f" {_['xlo']:.7f} {_['xhi']:.7f}  xlo xhi\n"
            + f" {_['ylo']:.7f} {_['yhi']:.7f}  ylo yhi\n"
            + f" {_['zlo']:.7f} {_['zhi']:.7f}  zlo zhi"
            + f"\n {_['xy']:.7f} {_['xz']:.7f} {_['yz']:.7f}  xy xz yz"
        )

        fmt_lmpdump = (
            f"ITEM: BOX BOUNDS xy xz yz {_['bx']} {_['by']} {_['bz']}\n"
            f"{_['xlo']:.7f} {_['xhi']:.7f} {_['xy']:.7f}  xlo xhi xy\n"
            + f"{_['ylo']:.7f} {_['yhi']:.7f} {_['xz']:.7f}  ylo yhi xz\n"
            + f"{_['zlo']:.7f} {_['zhi']:.7f} {_['yz']:.7f}  zlo zhi yz"
        )

        fmt_dcd = f"{_['a']:g} {_['cos_gamma']:g} {_['b']:g} {_['cos_beta']:g} {_['cos_alpha']:g} {_['c']:g}  a cos_gamma b cos_beta cos_alpha c"

        typ = self.alias.get(typ, typ)
        if typ in ('basis', 'vasp', 'poscar'):
            return fmt_basis
        elif typ in ('lattice', 'vmd'):
            return fmt_lattice
        elif typ == 'lmpdata':
            return fmt_lmpdata
        elif typ == 'lmpdump':
            return fmt_lmpdump
        elif typ == 'dcd':
            return fmt_dcd
        else:
            return (
                "\n# ----- input parameters (origin, bb-length, angle, boundary) -----\n"
                f"{self.input}\n"
                "\n# ----- basis Vectors -----\n"
                f"{fmt_basis}\n"
                "\n# ----- lattice Parameters -----\n"
                f"{fmt_lattice}\n"
                "# alpha is between b c, beta a c, gamma a b\n"
                "\n# ----- lammps data file -----\n"
                f"{fmt_lmpdata}\n"
                "\n# ----- lammps dump file -----\n"
                f"{fmt_lmpdump}\n"
                "\n# ----- dcd file ----\n"
                f"{fmt_dcd}\n"
            )

    # -----------------------------------------------

    def fractional_xyz(self, pts: np.ndarray) -> np.ndarray:
        pts = np.atleast_2d(pts)
        _ = self.output
        lo = np.array([_['xlo'], _['ylo'], _['zlo']])
        norm = np.sum(np.dot(_['v'], _['bn'].T) ** 2.0, axis=1) ** 0.5
        return np.dot(pts - lo, _['bn'].T) / norm

    def bbcheck(self, pts: np.ndarray) -> dict:
        """check which points in pts is within bounding box"""
        # TODO: pbc on selected faces
        # pbc = (flatten([pbc])*3)[:3]  # direction a, b, c

        pts = np.atleast_2d(pts)

        # lo and hi of each point
        _ = self.output
        lo = np.array([_['xlo'], _['ylo'], _['zlo']]) - pts
        hi = lo + np.sum(_['v'], axis=0)

        # normal vectors of box faces
        bn = _['bn']

        # distance from origin to box faces
        # (face_xlo face_ylo face_zlo face_xhi face_yhi face_zhi)
        dist = np.c_[
            np.dot(-bn, np.atleast_2d(lo).T).T,
            np.dot(bn, np.atleast_2d(hi).T).T,
        ]
        inside = np.min(dist, axis=1) >= 0
        N = pts.shape[0] - np.sum(inside)

        if N > 0:
            ix_outbound = np.where(~inside)[0]
            logger.debug(
                f"{N} points outside of bounding box, 0-index:\n{ix_outbound}"
            )

        return {
            'inbound': inside,
            'outbound': ~inside,
            'dist': dist,
        }

    def extend(self, pts: np.ndarray, bbcheck=None, pbc=False):
        """extend bounding box to accommodate pts, modify in-place"""

        box = self.output

        if bbcheck is None:
            bbcheck = self.bbcheck(pts)

        if np.sum(bbcheck['inbound']) == pts.shape[0]:
            return self

        pbc = (flatten([pbc]) * 3)[:3]  # direction a, b, c
        lo0 = np.array([box['xlo'], box['ylo'], box['zlo']])

        # edit the lo end
        lo1 = np.min(
            np.r_[pts[bbcheck['outbound']] - 1e-7, np.atleast_2d(lo0)], axis=0
        ) * (~np.array(pbc)).astype(int)
        self['x0'], self['y0'], self['z0'] = lo1
        shift = np.dot(box['u'], np.atleast_2d(lo1 - lo0).T).T

        # edit the hi end
        d = pts[bbcheck['outbound']] - lo0
        d_abc = np.dot(box['u'], np.atleast_2d(d).T).T
        v = box['u'] * (
            np.atleast_2d(
                np.maximum(
                    (np.max(d_abc, axis=0) + 1e-7)
                    * (~np.array(pbc)).astype(int),
                    np.sum(box['v'] ** 2.0, axis=1) ** 0.5,
                )
            ).T
            - shift.T
        )
        self.input.update(self._input_basis(v))

        return self

    def wrap(self, pts: np.ndarray, bbcheck=None, pbc=True):
        """move pts to wrap them within bounding box"""
        box = self.output
        if bbcheck is None:
            bbcheck = self.bbcheck(pts)
        if np.sum(bbcheck['inbound']) == pts.shape[0]:
            return pts
        pbc = (flatten(pbc) * 3)[:3]  # direction a, b, c
        rep = np.abs(
            np.floor_divide(
                bbcheck['dist'],
                np.tile(np.sum(box['v'] ** 2.0, axis=1) ** 0.5, 2),
            )
            * np.tile(pbc, 2).astype(int)
        )
        rep[bbcheck['dist'] >= 0] = 0
        shift = np.sum(
            np.multiply(
                np.ravel(rep).reshape(-1, 1),
                np.tile(np.r_[box['v'], -box['v']], (pts.shape[0], 1)),
            ).reshape(-1, 6, 3),
            axis=1,
        )
        return pts + shift

    def ghost(self, pts: np.ndarray, pbc=True):
        """get ghost pts in periodic images.

        Must wrap out-of-bound atoms first
        i.e.,  pts = box.wrap(pts)
               for pt in box.ghost(pts):
                   ....
        """
        box = self.output
        pbc = (flatten(pbc) * 3)[:3]  # direction a, b, c
        ref = pts  # np.copy(pts)
        L = np.array([box['a'] * pbc[0], box['b'] * pbc[1], box['c'] * pbc[2]])
        shift = L[0] * box['u'][0] + L[1] * box['u'][1] + L[2] * box['u'][2]
        side = (
            np.argmin(
                np.c_[
                    np.sum((pts - shift) ** 2.0, axis=1),
                    np.sum((pts + shift) ** 2.0, axis=1),
                ],
                axis=1,
            )
            * 2
            - 1
        )
        yield ref + np.outer(side, shift)
        for i in (0, 1, 2):
            shift = L[i] * box['u'][i]
            yield ref + np.outer(side, shift)
        for i, j in zip((0, 1, 2), (1, 2, 0)):
            shift = L[i] * box['u'][i] + L[j] * box['u'][j]
            yield ref + np.outer(side, shift)


if __name__ == '__main__':
    argv = ' '.join(sys.argv[1:])
    box = Box()
    typ = box.set_input(argv)
    print(f'input ({typ}): {argv}\n{box}')
