import math
import torch
import torch.nn.functional as F
from tqdm import tqdm
from torch_scatter import scatter_max, scatter
from ogb.nodeproppred import PygNodePropPredDataset, Evaluator
from torch_geometric.loader import NeighborSampler
from torch_geometric.nn import SAGEConv
from copy import deepcopy
import numpy as np
import sys
from utils import set_seeds, ns_graph_aug
from torch_geometric.utils import add_remaining_self_loops


import argparse
parser = argparse.ArgumentParser(description='OGBN-Products (SAGE)')
parser.add_argument('--seed', type=int, default=777, help='Random seed.')
parser.add_argument('--device', type=int, default=1)
parser.add_argument('--num_workers', type=int, default=12)

parser.add_argument('--epochs', type=int, default=80)#100
parser.add_argument('--runs', type=int, default=2)

parser.add_argument('--batch-size', type=int, default=1024)
parser.add_argument('--hidden_channels', type=int, default=256)
parser.add_argument('--num_layers', type=int, default=3)
parser.add_argument('--lr', type=float, default=0.0005)

parser.add_argument('--step-size', type=float, default=8e-3)
parser.add_argument('-m', type=int, default=3)
parser.add_argument('--test-freq', type=int, default=1)

parser.add_argument('--rate', type=float, default=0.5, help='数据增强扰动概率')
parser.add_argument('--par', type=float, default=1, help='对比损失系数')

parser.add_argument('--lam', type=float, default=0.01, help='约束损失系数')
parser.add_argument('--limt', type=float, default=0.0001, help='约束损失率')


args = parser.parse_args()
seed = args.seed
set_seeds(seed)
print(args)

dataset = PygNodePropPredDataset('ogbn-products')
split_idx = dataset.get_idx_split()
evaluator = Evaluator(name='ogbn-products')
data = dataset[0]

train_idx = split_idx['train']
test_idx = split_idx['test']
data.edge_index, _ = add_remaining_self_loops(data.edge_index)
train_loader = NeighborSampler(data.edge_index, node_idx=train_idx,
                               sizes=[24, 8, 4], batch_size=args.batch_size,
                               shuffle=True, num_workers=args.num_workers)


subgraph_loader = NeighborSampler(data.edge_index, node_idx=None, sizes=[-1],
                                  batch_size=4096, shuffle=False,
                                  num_workers=args.num_workers)


class SAGE(torch.nn.Module):
    def __init__(self, in_channels, hidden_channels, out_channels, num_layers):
        super(SAGE, self).__init__()

        self.num_layers = num_layers

        self.convs = torch.nn.ModuleList()

        self.convs.append(SAGEConv(in_channels, hidden_channels))

        for _ in range(num_layers - 2):
            self.convs.append(SAGEConv(hidden_channels, hidden_channels))

        self.convs.append(SAGEConv(hidden_channels, out_channels))

        self.fc1 = torch.nn.Linear(out_channels, hidden_channels)
        self.fc2 = torch.nn.Linear(hidden_channels, hidden_channels)
        self.tau = 0.4

    def reset_parameters(self):
        for conv in self.convs:
            conv.reset_parameters()

    def forward(self, x, adjs):
        # `train_loader` computes the k-hop neighborhood of a batch of nodes,
        # and returns, for each layer, a bipartite graph object, holding the
        # bipartite edges `edge_index`, the index `e_id` of the original edges,
        # and the size/shape `size` of the bipartite graph.
        # Target nodes are also included in the source nodes so that one can
        # easily apply skip-connections or add self-loops.
        for i, (edge_index, _, size) in enumerate(adjs):

            x_target = x[:size[1]]  # Target nodes are always placed first.
            if i == self.num_layers - 1:
                out = x

            x = self.convs[i]((x, x_target), edge_index)

            if i != self.num_layers - 1:

                x = F.relu(x)
                x = F.dropout(x, p=0.5, training=self.training)

        return x.log_softmax(dim=-1), x, out

    def inference(self, x_all):
        pbar = tqdm(total=x_all.size(0) * self.num_layers)
        pbar.set_description('Evaluating')

        # Compute representations of nodes layer by layer, using *all*
        # available edges. This leads to faster computation in contrast to
        # immediately computing the final representations of each batch.
        total_edges = 0
        for i in range(self.num_layers):
            xs = []
            for batch_size, n_id, adj in subgraph_loader:
                edge_index, _, size = adj.to(device)
                total_edges += edge_index.size(1)
                x = x_all[n_id].to(device)
                x_target = x[:size[1]]
                x = self.convs[i]((x, x_target), edge_index)
                if i != self.num_layers - 1:
                    x = F.relu(x)
                xs.append(x.cpu())

                pbar.update(batch_size)

            x_all = torch.cat(xs, dim=0)

        pbar.close()

        return x_all

    def negsam_loss(self, z1, z2, neg_mask):

        s_value = torch.exp(torch.mm(z1, z1.t()) / self.tau)
        b_value = torch.exp(torch.mm(z1, z2.t()) / self.tau)

        value_zi = b_value.diag().unsqueeze(0).T
        value_neg = (s_value + b_value) * neg_mask.float()
        value_neg = value_neg.sum(dim=1, keepdim=True)
        neg_sum = 2 * neg_mask.sum(dim=1, keepdim=True)
        value_neg = value_neg
        value_neg = torch.max(value_neg, neg_sum * math.exp(-1.0 / self.tau))
        value_mu = value_zi + value_neg

        loss = -torch.log(value_zi / value_mu)
        return loss

    def jsd_loss(self, enc1, enc2, pos_mask, neg_mask):
        logits = enc1 @ enc2.t()
        Epos = (np.log(2.) - F.softplus(- logits))
        Eneg = (F.softplus(-logits) + logits - np.log(2.))
        #print("x:",enc1.shape,"g:",enc2.shape,"pos:",pos_mask.shape,"neg:",neg_mask.shape)
        Epos = (Epos * pos_mask).sum() / pos_mask.sum()
        Eneg = (Eneg * neg_mask).sum() / neg_mask.sum()
        return Eneg - Epos

    def projection(self, z):
        z = F.elu(self.fc1(z))
        return self.fc2(z)

    def cl_lossaug(self, z1, g2, pos_mask, neg_mask):
        h1 = self.projection(z1)
        #h2 = self.projection(z2)
        h1 = F.normalize(h1)
        #h2 = F.normalize(h2)

        ret = model.jsd_loss(h1, g2, pos_mask, neg_mask)

        ret = ret.mean()

        return ret

device = f'cuda:{args.device}' if torch.cuda.is_available() else 'cpu'
device = torch.device(device)
model = SAGE(dataset.num_features, args.hidden_channels, dataset.num_classes, args.num_layers)
model = model.to(device)

x = data.x.to(device)
y = data.y.squeeze().to(device)


def graph_em(g, neighbor, cluster):
    neighbor_emb = g[neighbor].to(device)
    g_dim = cluster.max() + 1
    # if g_dim < 1024 and g_dim >1010:
    #     g_dim = 1024
    graph_embedding = scatter(neighbor_emb, cluster, dim=0, dim_size=g_dim, reduce='mean')

    return graph_embedding


def train_products(model, clean, y, adjs, adja, args, optimizer, device, criterion, train_idx=None) :
    model.train()
    cluster = adjs[2][0][1]
    neighbor = adjs[2][0][0]

    if train_idx is not None:
        model_forward = lambda x: model(x, adjs)[train_idx]
    else:
        model_forward1 = lambda x: model(x, adjs)
        model_forward2 = lambda x: model(x, adja)
    optimizer.zero_grad()


    out, x1, g1 = model_forward1(clean)
    aug_pre, x2, g2 = model_forward2(clean)

    g1 = graph_em(g1, neighbor, cluster)
    g2 = graph_em(g2, neighbor, cluster)

    label = y.contiguous().view(-1,1)

    pos_mask = torch.eq(label, label.T).float().to(device)
    neg_mask = 1 - pos_mask
    loss1 = model.cl_lossaug(x1, g2, pos_mask, neg_mask)
    loss2 = model.cl_lossaug(x2, g1, pos_mask, neg_mask)

    loss_cl = (loss1 + loss2) / 10

    loss_train = criterion(aug_pre, y)
    loss = loss_train + args.par * loss_cl


    # aug_pre = aug_pre
    # aug_y = y
    aug_loss = loss

    # loss /= args.m
    #
    # for _ in range(args.m-1):
    #     loss.backward()
    #     out, _ = model_forward1(clean)
    #     loss = criterion(out, y)
    #     loss /= args.m
    loss.backward()
    optimizer.step()

    #print(f'Batch:{i},loss_train:{loss_train}, loss_cl:{loss_cl}, loss:{loss}')

    return loss_train, out, aug_loss


def train(epoch,  args):
    total_loss = total_correct = 0
    # rate = [1/2, 1/4, 1/6]
    # rate = 1/2
    rate = args.rate
    print("rate:", rate)
    aug = []
    i=0

    for batch_size, n_id, adjs in train_loader:
        # `adjs` holds a list of `(edge_index, e_id, size)` tuples.
        i = i + 1

        adjs = [adj.to(device) for adj in adjs]

        adj_aug = deepcopy(adjs)
        adja = ns_graph_aug(adj_aug, device, rate)

        # print("rate1", rate)

        clean = x[n_id]

        loss, out, aug_loss = train_products(model, clean, y[n_id[:batch_size]], adjs, adja, args, optimizer, device,
                                          F.nll_loss)

        aug.append(aug_loss)
        # print("aug_loss:", aug_loss)

        if i >= 2:
            if aug[i - 1] < aug[i - 2]:
                rate = rate + args.limt * (aug[i - 2] - aug[i - 1])

            elif aug[i - 1] > aug[i - 2]:
                rate = rate - args.limt * (aug[i - 1] - aug[i - 2])

        # print("rate2", rate)

        total_loss += float(loss)

        total_correct += int(out.argmax(dim=-1).eq(y[n_id[:batch_size]]).sum())


    loss = total_loss / len(train_loader)
    approx_acc = total_correct / train_idx.size(0)

    rate_epoch = rate
    print('rate_epoch:', rate_epoch)
    print(args.limt)
    with open('./rate_productsage.txt', 'a', encoding='utf-8') as f:
            f.write("%.4f" % rate_epoch)
            f.write(',')


    print(f'Epoch:{epoch:}, Loss:{loss:.4f}, Train acc:{approx_acc:.4f}')

    return loss, approx_acc, rate_epoch


@torch.no_grad()
def test():
    model.eval()

    out = model.inference(x)

    y_true = y.cpu().unsqueeze(-1)
    y_pred = out.argmax(dim=-1, keepdim=True)
    # test_loss = F.nll_loss(out, y)
    # print(test_loss)
    train_acc = evaluator.eval({
        'y_true': y_true[split_idx['train']],
        'y_pred': y_pred[split_idx['train']],
    })['acc']
    val_acc = evaluator.eval({
        'y_true': y_true[split_idx['valid']],
        'y_pred': y_pred[split_idx['valid']],
    })['acc']
    test_acc = evaluator.eval({
        'y_true': y_true[split_idx['test']],
        'y_pred': y_pred[split_idx['test']],
    })['acc']


    return train_acc, val_acc, test_acc


vals, tests = [], []
for run in range(args.runs):
    best_val, final_test = 0, 0

    model.reset_parameters()
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
    args.rate = 0.5
    for epoch in range(1, args.epochs+1):
        loss, acc, rate_epoch= train(epoch, args)
        args.rate = rate_epoch
        if epoch >100 and epoch % args.test_freq == 0 or epoch == args.epochs:
            result = test()
            tra, val, tst = result
            print(f'Epoch:{epoch}, train:{tra:.6f}, val:{val:.6f}, test:{tst:.6f}')
            if val > best_val:
                best_val = val
                final_test = tst
    print(f'Run{run} val:{best_val:.6f}, test:{final_test:.6f}')
    vals.append(best_val)
    tests.append(final_test)

print('')
print("test:", tests)
print(f"Average val accuracy: {np.mean(vals)} ± {np.std(vals):.6f}")
print(f"Average test accuracy: {np.mean(tests)} ± {np.std(tests):.6f}")
print(args)