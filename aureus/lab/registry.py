"""实验登记表：命令行只从这里查找可运行的实验（D82）。"""

from aureus.lab.calibration import CALIBRATION_EXPERIMENT
from aureus.lab.experiment import Experiment

EXPERIMENTS: dict[str, Experiment] = {
    CALIBRATION_EXPERIMENT.name: CALIBRATION_EXPERIMENT,
}
