"""Forward kinematics of the robots the record comparison knows (design 12 §8.7, D-E16).

DEMO ships one model: the Franka Emika Panda (the Franka Research 3 has the same kinematics), in
Craig's modified Denavit-Hartenberg convention - ``T_i = RotX(alpha_{i-1}) TransX(a_{i-1}) RotZ(theta_i)
TransZ(d_i)``. The chain ends at the flange ``panda_link8`` (d = 0.107 m); the hand ``panda_hand`` is the
flange turned -45 deg about z and its TCP ``panda_hand_tcp`` is 0.1034 m further along z. On DROID
(galbot dataset2) the chain reproduces ``observation.state.cartesian_position`` exactly. Other robots come
later from a URDF, not written out here by hand.
"""
from __future__ import annotations

import dataclasses

import numpy as np


@dataclasses.dataclass(frozen=True)
class Robot:
    name: str
    joints: int
    mdh: tuple[tuple[float, float, float], ...]        # (a_{i-1} m, d_i m, alpha_{i-1} rad) per joint
    tip: tuple[float, float, float]                    # (a, d, alpha) of the fixed link to the tip frame
    tip_frame: str
    frames: dict[str, tuple[str, np.ndarray]]          # named fixed frames: name -> (parent, T_parent_frame)

    def fk(self, q: np.ndarray) -> np.ndarray:
        """(N, joints) radians -> (N, 4, 4) poses of ``tip_frame`` in the robot base."""
        q = np.atleast_2d(np.asarray(q, float))
        if q.shape[1] != self.joints:
            raise ValueError(f"{self.name}: {self.joints} joint values per frame, got {q.shape[1]}")
        T = np.tile(np.eye(4), (len(q), 1, 1))
        for i, (a, d, alpha) in enumerate(self.mdh):
            T = T @ _mdh(a, d, alpha, q[:, i])
        return T @ _mdh(*self.tip, np.zeros(len(q)))


def _mdh(a: float, d: float, alpha: float, theta: np.ndarray) -> np.ndarray:
    ca, sa = np.cos(alpha), np.sin(alpha)
    ct, st = np.cos(theta), np.sin(theta)
    T = np.zeros((len(theta), 4, 4))
    T[:, 0, 0], T[:, 0, 1], T[:, 0, 3] = ct, -st, a
    T[:, 1, 0], T[:, 1, 1], T[:, 1, 2], T[:, 1, 3] = st * ca, ct * ca, -sa, -d * sa
    T[:, 2, 0], T[:, 2, 1], T[:, 2, 2], T[:, 2, 3] = st * sa, ct * sa, ca, d * ca
    T[:, 3, 3] = 1.0
    return T


def _fixed(rz_deg: float = 0.0, z_m: float = 0.0) -> np.ndarray:
    c, s = np.cos(np.radians(rz_deg)), np.sin(np.radians(rz_deg))
    T = np.eye(4)
    T[:2, :2] = [[c, -s], [s, c]]
    T[2, 3] = z_m
    return T


_PANDA = dict(
    joints=7,
    mdh=((0.0, 0.333, 0.0), (0.0, 0.0, -np.pi / 2), (0.0, 0.316, np.pi / 2), (0.0825, 0.0, np.pi / 2),
         (-0.0825, 0.384, -np.pi / 2), (0.0, 0.0, np.pi / 2), (0.088, 0.0, np.pi / 2)),
    tip=(0.0, 0.107, 0.0), tip_frame="panda_link8",
    frames={"panda_hand": ("panda_link8", _fixed(rz_deg=-45.0)),
            "panda_hand_tcp": ("panda_hand", _fixed(z_m=0.1034))})

ROBOTS: dict[str, Robot] = {
    "franka_panda": Robot(name="franka_panda", **_PANDA),
    "franka_fr3": Robot(name="franka_fr3", **_PANDA),
}


def get(name: str) -> Robot:
    try:
        return ROBOTS[name]
    except KeyError:
        raise KeyError(f"robot model {name!r} is not built in (known: {', '.join(ROBOTS)})") from None
