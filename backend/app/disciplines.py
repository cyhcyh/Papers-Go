"""The eight arXiv groups shared by sources and research-area proposals."""
from typing import Literal

Discipline = Literal['Computer Science','Mathematics','Statistics','Physics',
                     'Electrical Engineering and Systems Science','Economics',
                     'Quantitative Biology','Quantitative Finance']
LABELS = {'Computer Science':'计算机科学','Mathematics':'数学','Statistics':'统计学',
          'Physics':'物理学','Electrical Engineering and Systems Science':'电气工程与系统科学',
          'Economics':'经济学','Quantitative Biology':'定量生物学','Quantitative Finance':'定量金融'}
DISCIPLINES = tuple(LABELS)
ID_PREFIXES = {'Computer Science':'CS','Mathematics':'MATH','Statistics':'STAT','Physics':'PHYS',
               'Electrical Engineering and Systems Science':'EESS','Economics':'ECON',
               'Quantitative Biology':'QBIO','Quantitative Finance':'QFIN'}
