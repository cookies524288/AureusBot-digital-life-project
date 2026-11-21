"""实验登记表：命令行只从这里查找可运行的实验（D82）。"""

from aureus.lab.experiment import Experiment

EXPERIMENTS: dict[str, Experiment] = {}
