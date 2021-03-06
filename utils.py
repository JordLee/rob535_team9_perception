from pathlib import Path
import torch
import csv
from pdb import set_trace
import numpy as np

from tqdm import tqdm
import torch.nn.functional as F

def get_classes_to_label_map():
    # Loads the CSV for converting 23 classes to 3 classes
    with open('classes.csv', 'r') as class_key:
      reader = csv.reader(class_key)
      list_mapping = list(reader)[1:]

    new_list_mapping = {}
    for i, x in enumerate(list_mapping):
      new_list_mapping[i] = int(x[-1])
    return new_list_mapping


list_mapping = get_classes_to_label_map()


def class_shrinker(inp, target):
  new_p_vals = torch.zeros(inp.shape[0], 3).cuda() # TODO hard coded
  new_t_vals = target.clone()

  for x in range(inp.shape[1]): # For each class currenetly existing
    new_p_vals[:, list_mapping[x]] += inp[:, x] # Mapping to the new class

  for x in range(inp.shape[0]):
    new_t_vals[x] = list_mapping[int(target[x])]
  return new_p_vals, new_t_vals


def sum_cross_entropy(inp, target):
  new_p_vals, new_t_vals = class_shrinker(inp, target)
  return F.cross_entropy(inp, target) + 3.0 * F.cross_entropy(new_p_vals, new_t_vals)

def sum_mse(inp, target):
  return F.mse_loss(inp.float(), target.float())


class Runner(object):
    cuda = torch.cuda.is_available()
    torch.backends.cudnn.benchmark = True

    def __init__(self, model, optimizer, loss_f, task, save_dir=None, save_freq=5):
        self.model = model
        if self.cuda:
            model.cuda()
        self.optimizer = optimizer
        self.loss_f = loss_f
        self.save_dir = save_dir
        self.save_freq = save_freq
        self.epoch = 0
        self.best_acc = -100
        self.task = task

    def _iteration(self, data_loader, batch_size, is_train=True):
        loop_loss = []
        accuracy = []
        accuracy_shrunk = []
        outputs = []
        outputs_data = []
        pbar = tqdm(data_loader, ncols=40, disable=False)
        ct = 0
        for i, (path, data, target) in enumerate(pbar):
            if self.cuda:
                data, target = data.cuda(), target.cuda()
            output = self.model(data)

            # Testing is with batch_size 1
            if not is_train:
                for p in range(len(path)):
                  #outputs.append((path[p], int(output.data.max(1)[1][p])))
                  outputs.append((path[p], output.data[p].cpu().numpy()))
                  outputs_data.append((path[p], torch.nn.functional.softmax(output.data[p, :]).cpu().numpy()))

            loss = self.loss_f(output, target)
            loop_loss.append(loss.data.item() / len(data_loader))

            if self.task == 2:
                accuracy_shrunk.append(((output.data.float() -target.data.float())**2/len(target.data.float())).sum().item())
                accuracy.append(((output.data.float()  - target.data.float())**2/len(target.data.float())).sum().item())
            else:
                new_o, new_t = class_shrinker(output.data, target.data)
                accuracy_shrunk.append((new_o.max(1)[1] == new_t).sum().item())
                accuracy.append((output.data.max(1)[1] == target.data).sum().item())
            if is_train:
                self.optimizer.zero_grad()
                loss.backward()
                self.optimizer.step()

            # Fetch LR
            lr = 0.0
            for param_group in self.optimizer.param_groups:
              lr = param_group['lr']

            # Set Progress bar
            if self.task ==2:
                pbar.set_description(
                "{} epoch {}: itr {:<5}/ {} - loss {:.3f} - error {:.2f} - error3 {:.2f} - lr {:.4f}"
                .format('TRAIN' if is_train else 'TEST ', self.epoch, i*batch_size, len(data_loader)*batch_size, loss.data.item(), (sum(accuracy) / ((i+1)*batch_size))*100.0, (sum(accuracy_shrunk) / ((i+1)*batch_size))*100.0, lr))

            else:
                pbar.set_description(
                 "{} epoch {}: itr {:<5}/ {} - loss {:.3f} - acc {:.2f}% - acc3 {:.2f}% - lr {:.4f}"
                 .format('TRAIN' if is_train else 'TEST ', self.epoch, i*batch_size, len(data_loader)*batch_size, loss.data.item(), (sum(accuracy) / ((i+1)*batch_size))*100.0, (sum(accuracy_shrunk) / ((i+1)*batch_size))*100.0, lr))

        mode = "train" if is_train else "test/val"
        if mode == "test/val":
          with open('csvs/test_track.csv', 'a') as f:
            f.write(f">>>[{mode}] epoch: {self.epoch} loss: {sum(loop_loss):.2f}/accuracy: {sum(accuracy_shrunk) / len(data_loader.dataset):.2%}\n")
        if is_train:
          return loop_loss, accuracy_shrunk, None, None
        else:
          return loop_loss, accuracy_shrunk, outputs, outputs_data

    def train(self, data_loader, batch_size):
        self.model.train()
        with torch.enable_grad():
            loss, accuracy, _, _ = self._iteration(data_loader, batch_size)

    def test(self, data_loader, batch_size):
        self.model.eval()
        with torch.no_grad():
            loss, accuracy, outputs, logits = self._iteration(data_loader, batch_size, is_train=False)
        return loss, accuracy, outputs, logits

    def loop(self, epochs, train_data, more_train_data, test_data, scheduler, batch_size):
        for ep in range(1, epochs + 1):
            self.epoch = ep
            print("training one epoch on new data")
            self.train(more_train_data, batch_size)
            loss, accuracy, outputs, logits = self.test(test_data, batch_size)
            if scheduler is not None:
                scheduler.step(sum(loss))
            self.save(str(ep+99), accuracy)

            print("training one epoch on original data")
            self.train(train_data, batch_size)
            loss, accuracy, outputs, logits = self.test(test_data, batch_size)
            if scheduler is not None:
                scheduler.step(sum(loss))
            self.save(str(ep), accuracy)
        return self.best_acc

    def save(self, epoch, acc, **kwargs):
        if self.save_dir is not None:
            model_out_path = Path(self.save_dir)
            state = {"epoch": epoch, "weight": self.model.state_dict()}
            if not model_out_path.exists():
                model_out_path.mkdir()
            if self.best_acc < sum(acc):
                torch.save(state, model_out_path / "model_epoch_9999.pth")
                self.best_acc = sum(acc)
            torch.save(state, model_out_path / "model_epoch_{}.pth".format(epoch))
