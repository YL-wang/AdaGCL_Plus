# AdaGCL+

<div align="center">
  <img src="https://raw.githubusercontent.com/UB-GOLD/UB-GOLD/main/Image/intro.jpg" width="900px"/>
</div>

## Experiments

### Requirements

* ogb>=1.3.3
* torch>=1.10.0
* torch-geometric>=2.0.4

### Training

GraphSAINT <br>
``python saint_graph.py --epochs <epochs> --load_CL <load_CL> --par <mu> --rate <rate> --topk <topk> --limt <delta>``
<br>
where ``<mu>`` is a Node-IB loss ratio. ``<rate>`` is the initial perturbation ratio of data augmentation.
``<topk>`` is the number of subgraphs involved in contrastive learning. ``<load_CL>`` is to add contrastive learning at the Nth epoch, default is 0.
``<delta>`` is AutoR’s penalty intensity.

Cluster-GCN <br>
``python cluster_graph.py --epochs <epochs> --load_CL <load_CL> --par <mu> --rate <rate> --limt <delta>``
<br>

GraphSAGE <br>
``python ns_graph.py --epochs <epochs> --par <mu> --rate <rate> --limt <delta>``


## Citation
If you find our repository useful for your research, please consider citing our paper:
```
@article{wang2025adagcl+,
  title={AdaGCL+: An Adaptive Subgraph Contrastive Learning Towards Tackling Topological Bias},
  author={Wang, Yili and Liu, Yaohua and Liu, Ninghao and Miao, Rui and Wang, Ying and Wang, Xin},
  journal={IEEE Transactions on Pattern Analysis and Machine Intelligence},
  year={2025},
  publisher={IEEE}
}

@inproceedings{wang2022adagcl,
title={AdaGCL: Adaptive Subgraph Contrastive Learning to Generalize Large-scale Graph Training},
author={Wang, Yili and Zhou, Kaixiong and Miao, Rui and Liu, Ninghao and Wang, Xin},
booktitle={Proceedings of the 31st ACM International Conference on Information & Knowledge Management},
pages={2046--205},
year={2022}
}

```

