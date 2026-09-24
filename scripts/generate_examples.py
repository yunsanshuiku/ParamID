"""Generate reproducible, explicitly synthetic identification experiments."""

import json
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def main():
    rng = np.random.default_rng(20260916)
    target = ROOT / "examples"
    target.mkdir(exist_ok=True)
    n, dt = 900, 0.01
    t = np.arange(n) * dt
    x1, x2 = rng.normal(size=(2, n))
    y = 2.5 * x1 - 0.7 * x2 + 0.3 + rng.normal(0, .025, n)
    np.savetxt(target / "regression.csv", np.column_stack((t, x1, x2, y)), delimiter=",", header="time_s,x1,x2,y", comments="", fmt="%.10g")
    regression = {"model": {"kind": "regression", "output": "y", "features": ["x1", "x2"], "time": "time_s", "sample_time": dt, "intercept": True}, "algorithm": "ls"}
    u = np.repeat(rng.choice([-1., 1.], size=(n + 6)//7), 7)[:n]
    output = np.zeros(n)
    for k in range(1, n):
        output[k] = .82 * output[k - 1] + .35 * u[k - 1] + .04 + rng.normal(0, .005)
    np.savetxt(target / "arx.csv", np.column_stack((t, u, output)), delimiter=",", header="time_s,u,y", comments="", fmt="%.10g")
    arx = {"model": {"kind": "arx", "output": "y", "input": "u", "time": "time_s", "sample_time": dt, "intercept": True}, "algorithm": "ls"}
    for name, config in (("regression", regression), ("arx", arx)):
        (target / f"{name}.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    (target / "truth.json").write_text(json.dumps({"synthetic": True, "seed": 20260916,
        "regression": {"x1": 2.5, "x2": -.7, "intercept": .3},
        "arx": {"a1": .82, "b0": .35, "intercept": .04, "noise": "white equation innovation, std=0.005"}}, indent=2), encoding="utf-8")
    print(f"Generated synthetic examples in {target}")


if __name__ == "__main__":
    main()
