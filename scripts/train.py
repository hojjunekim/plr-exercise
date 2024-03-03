from __future__ import print_function
import torch
import torch.nn.functional as F
import torch.optim as optim
from torchvision import datasets, transforms
from torch.optim.lr_scheduler import StepLR
from plr_exercise.models.cnn import Net
from plr_exercise import PLR_ROOT_DIR
import wandb
import optuna
import os
from omegaconf import DictConfig, OmegaConf
import hydra
import time


def train(args, model, device, train_loader, optimizer, epoch):
    """
    Trains the model for one epoch.

    Args:
        args (argparse.Namespace): Command-line arguments.
        model (torch.nn.Module): The model to be trained.
        device (torch.device): The device to be used for training.
        train_loader (torch.utils.data.DataLoader): The data loader for training data.
        optimizer (torch.optim.Optimizer): The optimizer to be used for training.
        epoch (int): The current epoch number.
    """
    model.train()
    start_time = time.time()  # Start measuring time
    for batch_idx, (data, target) in enumerate(train_loader):
        data, target = data.to(device), target.to(device)
        optimizer.zero_grad()
        output = model(data)
        loss = F.nll_loss(output, target)
        loss.backward()
        optimizer.step()
        if batch_idx % args["log_interval"] == 0:
            print(
                "Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}".format(
                    epoch,
                    batch_idx * len(data),
                    len(train_loader.dataset),
                    100.0 * batch_idx / len(train_loader),
                    loss.item(),
                )
            )
            wandb.log({"epoch": epoch, "train_loss": loss.item()})
            if args["dry_run"]:
                break
    end_time = time.time()  # Stop measuring time
    train_time = end_time - start_time
    print("Train Time: {:.2f} seconds".format(train_time))


def test(model, device, test_loader, epoch):
    """
    Evaluates the model on the test dataset.

    Args:
        model (torch.nn.Module): The model to be evaluated.
        device (torch.device): The device to be used for evaluation.
        test_loader (torch.utils.data.DataLoader): The data loader for test data.
        epoch (int): The current epoch number.
    """
    model.eval()
    start_time = time.time()  # Start measuring time
    test_loss = 0
    correct = 0

    with torch.no_grad():
        for data, target in test_loader:
            data, target = data.to(device), target.to(device)
            output = model(data)
            test_loss += F.nll_loss(output, target, reduction="sum").item()  # sum up batch loss
            pred = output.argmax(dim=1, keepdim=True)  # get the index of the max log-probability
            correct += pred.eq(target.view_as(pred)).sum().item()

    test_loss /= len(test_loader.dataset)

    end_time = time.time()  # Stop measuring time
    test_time = end_time - start_time
    print("Test Time: {:.2f} seconds".format(test_time))

    print(
        "\nTest set: Average loss: {:.4f}, Accuracy: {}/{} ({:.0f}%)\n".format(
            test_loss, correct, len(test_loader.dataset), 100.0 * correct / len(test_loader.dataset)
        )
    )
    wandb.log({"test_loss": test_loss, "epoch": epoch})


def objective(trial, args, train_loader, test_loader):
    """
    Objective function for Optuna optimization.

    Args:
        trial (optuna.Trial): The current Optuna trial.
        args (argparse.Namespace): Command-line arguments.
        train_loader (torch.utils.data.DataLoader): The data loader for training data.
        test_loader (torch.utils.data.DataLoader): The data loader for test data.

    Returns:
        float: The accuracy of the model.
    """
    learning_rate = trial.suggest_float("learning_rate", 1e-5, 1e-1)
    epochs = trial.suggest_int("epochs", 1, 10)

    device = torch.device("cpu")
    model = Net().to(device)
    optimizer = optim.Adadelta(model.parameters(), lr=learning_rate)

    for epoch in range(epochs):
        train(args, model, device, train_loader, optimizer, epoch)
        test(model, device, test_loader, epoch)

    test_loss = 0
    correct = 0
    with torch.no_grad():
        for data, target in test_loader:
            data, target = data.to(device), target.to(device)
            output = model(data)
            test_loss += F.nll_loss(output, target, reduction="sum").item()
            pred = output.argmax(dim=1, keepdim=True)
            correct += pred.eq(target.view_as(pred)).sum().item()
    accuracy = correct / len(test_loader.dataset)

    return accuracy


@hydra.main(config_path="../config/", config_name="config")
def main(cfg: DictConfig):
    """
    Main function for training the model.
    """
    args = OmegaConf.to_container(cfg, resolve=True)

    wandb.login()
    os.makedirs(os.path.join(PLR_ROOT_DIR, "results"), exist_ok=True)
    run = wandb.init(
        dir=os.path.join(PLR_ROOT_DIR, "results"),
        project="plr-project",
        config=args,
        settings=wandb.Settings(code_dir=PLR_ROOT_DIR),
    )
    include_fn = lambda path, root: path.endswith(".py") or path.endswith(".yaml")
    run.log_code(name="source_files", root=PLR_ROOT_DIR, include_fn=include_fn)

    use_cuda = not args["no_cuda"] and torch.cuda.is_available()

    torch.manual_seed(args["seed"])

    if use_cuda:
        device = torch.device("cuda")
    else:
        device = torch.device("cpu")

    train_kwargs = {"batch_size": args["batch_size"]}
    test_kwargs = {"batch_size": args["test_batch_size"]}
    if use_cuda:
        cuda_kwargs = {"num_workers": 1, "pin_memory": True, "shuffle": True}
        train_kwargs.update(cuda_kwargs)
        test_kwargs.update(cuda_kwargs)

    transform = transforms.Compose([transforms.ToTensor(), transforms.Normalize((0.1307,), (0.3081,))])
    dataset1 = datasets.MNIST("../data", train=True, download=True, transform=transform)
    dataset2 = datasets.MNIST("../data", train=False, transform=transform)
    train_loader = torch.utils.data.DataLoader(dataset1, **train_kwargs)
    test_loader = torch.utils.data.DataLoader(dataset2, **test_kwargs)

    # Set up Optuna study
    study = optuna.create_study(direction="maximize")
    study.optimize(lambda trial: objective(trial, args, train_loader, test_loader), n_trials=10)

    # objective_partial = partial(objective, args, train_loader, test_loader)
    # study.optimize(objective_partial, n_trials=10)  # You can adjust n_trials as needed

    # Get best hyperparameters
    best_params = study.best_params
    best_lr = best_params["lr"]
    best_epochs = best_params["epochs"]

    print("Best learning rate:", best_lr)
    print("Best number of epochs:", best_epochs)

    # Use best hyperparameters for final training
    model = Net().to(device)
    optimizer = optim.Adam(model.parameters(), lr=best_lr)
    scheduler = StepLR(optimizer, step_size=1, gamma=args["gamma"])
    for epoch in range(best_epochs):
        train(args, model, device, train_loader, optimizer, epoch)
        test(model, device, test_loader, epoch)
        scheduler.step()

    if args["save_model"]:
        torch.save(model.state_dict(), "mnist_cnn.pt")

    wandb.finish()


if __name__ == "__main__":
    main()
