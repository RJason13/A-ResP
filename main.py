from __future__ import absolute_import, division, print_function, unicode_literals
import os
import argparse
import heapq

import logging
import download
from losses import kld, nss, cc, auc_borji, kld_cc

import tensorflow as tf
from tensorflow.keras import backend
from tensorflow.keras.utils import Progbar

import config
import data
from model import MyModel

os.environ["TF_CPP_MIN_LOG_LEVEL"] = "2"
logging.getLogger("tensorflow").setLevel(logging.ERROR)
loss_fn_name = config.PARAMS["loss_fn"]

def _update_metrics(metrics_wrapper, y_true, y_fixs_true, y_pred):
    for name, value in _calc_metrics(metrics_wrapper.keys(), y_true, y_fixs_true, y_pred).items():
        metrics_wrapper[name](value)

def _calc_metrics(metrics, y_true, y_fixs_true, y_pred):
    d = {}
    for name in metrics:
        inputs = []
        inputs.append(y_true if config.MET_SPECS[name] == "m" else y_fixs_true)
        inputs.append(y_pred)
        d[name] = (globals().get(name, None)(*inputs))
    return d


def _print_metrics(res_metrics):
    res_printer = lambda x: "{}: {}".format(x[0], ('%.4f' if x[1].result() > 1e-3 else '%.4e') % x[1].result())
    return " - ".join(list(map(res_printer, res_metrics.items())))

def define_paths(current_path, args):
    """A helper function to define all relevant path elements for the
       locations of data, weights, and the results from either training
       or testing a model.

    Args:
        current_path (str): The absolute path string of this script.
        args (object): A namescpace object with values from command line.

    Returns:
        dict: A dictionary with all path elements.
    """

    if os.path.isfile(args.path):
        data_path = args.path
    else:
        data_path = os.path.join(args.path, "")

    results_path = current_path + "/results/"
    weights_path = current_path + "/weights/"
    ckpts_path = weights_path + "ckpts/"

    if args.action in ["train", "eval", "find_n_high"]:
        if args.data not in data_path:
            data_path += args.data + "/"

    paths = {
        "data": data_path,
        "results": results_path,
        "weights": weights_path,
        "ckpts": ckpts_path
    }

    if("weights" in args):
        if not args.weights is None:
            paths["trained_weights"] = current_path + "/" + args.weights

    return paths

@tf.function
def train_step(images, y_true, model, loss_fn, optimizer):
    with tf.GradientTape() as tape:
        y_pred = model(images)
        loss = loss_fn(y_true, y_pred)
    gradients = tape.gradient(loss, model.trainable_variables)
    optimizer.apply_gradients(zip(gradients, model.trainable_variables))
    return y_pred, loss

@tf.function
def val_step(images, y_true, model, loss_fn):
    y_pred = model(images)
    loss = loss_fn(y_true, y_pred)
    return y_pred, loss

@tf.function
def test_step(images, model):
    return model(images)

def train_model(ds_name, encoder, paths):
    """The main function for executing network training. It loads the specified
       dataset iterator, saliency model, and helper classes. Training is then
       performed in a new session by iterating over all batches for a number of
       epochs. After validation on an independent set, the model is saved and
       the training history is updated.

    Args:
        ds_name (str): Denotes the dataset to be used during training.
        paths (dict, str): A dictionary with all path elements.
    """

    w_filename_template = "/%s_%s_%s_weights.h5" # [encoder]_[ds_name]_[loss_fn_name]_weights.h5

    (train_ds, n_train), (val_ds, n_val) = data.load_train_dataset(ds_name, paths["data"])
    
    print(">> Preparing model with encoder %s..." % encoder)

    model = MyModel(encoder, ds_name, "train")

    if ds_name != "salicon":
        salicon_weights = paths["weights"] + w_filename_template % (encoder, "salicon", loss_fn_name)
        if os.path.exists(salicon_weights):
            print("Salicon weights are loaded!\n    %s"%salicon_weights)
        else:
            download.download_pretrained_weights(paths["weights"], encoder, "salicon", loss_fn_name)
        model.load_weights(salicon_weights)
        del salicon_weights

    model.summary()

    n_epochs = config.PARAMS["n_epochs"]

    # Preparing
    loss_fn = globals().get(loss_fn_name, None)
    optimizer = tf.keras.optimizers.Adam(config.PARAMS["learning_rate"])

    train_metric = tf.keras.metrics.Mean(name="train_loss")
    val_metric = tf.keras.metrics.Mean(name="val_loss")

    ckpts_path = paths["ckpts"] + "%s/%s/%s/" % (encoder, ds_name, loss_fn_name)
    ckpt = tf.train.Checkpoint(net=model, train_metric=train_metric, val_metric=val_metric)
    ckpt_manager = tf.train.CheckpointManager(ckpt, ckpts_path, max_to_keep=n_epochs)
    start_epoch = 0
    
    # if a checkpoint exists, restore the latest checkpoint.
    if ckpt_manager.latest_checkpoint:
        ckpt.restore(ckpt_manager.latest_checkpoint).assert_consumed()
        start_epoch = int(ckpt_manager.latest_checkpoint.split('-')[-1])
        print ('Checkpoint restored:\n{}'.format(ckpt_manager.latest_checkpoint))
        train_metric.reset_states()
        val_metric.reset_states()

    print("\n>> Start training model on %s..." % ds_name.upper())
    print(("Training details:" +
    "\n{0:<4}Number of epochs: {n_epochs:d}" +
    "\n{0:<4}Batch size: {batch_size:d}" +
    "\n{0:<4}Learning rate: {learning_rate:.1e}" +
    "\n{0:<4}Loss function: {1}").format(" ", loss_fn_name, **config.PARAMS))
    print("_" * 65)
    if ds_name == "salicon" and start_epoch < 2:
        model.freeze_unfreeze_encoder_trained_layers(True)
    for epoch in range(start_epoch, n_epochs):
        if ds_name == "salicon" and epoch == 2:
            model.freeze_unfreeze_encoder_trained_layers(False)

        train_progbar = Progbar(n_train, stateful_metrics=["train_loss"])
        for train_x, train_y_true, train_ori_sizes, train_filenames in train_ds:
            train_y_pred, train_loss = train_step(train_x, train_y_true, model, loss_fn, optimizer)
            train_metric(train_loss)
            train_progbar.add(train_x.shape[0], [("train_loss", train_metric.result())])

        val_progbar = Progbar(n_val, stateful_metrics=["val_loss"])
        for val_x, val_y_true, val_ori_sizes, val_filenames in val_ds:
            val_y_pred, val_loss = val_step(val_x, val_y_true, model, loss_fn)
            val_metric(val_loss)
            val_progbar.add(val_x.shape[0], [("val_loss", val_metric.result())])

        train_metrics_results = _print_metrics({"train_loss": train_metric})
        val_metrics_results = _print_metrics({"val_loss": val_metric})
        print('Epoch {} - {} - {}'.format(epoch+1, train_metrics_results, val_metrics_results))
        
        ckpt_manager.save()

        # Reset the metrics for the next epoch
        train_metric.reset_states()
        val_metric.reset_states()

    # Picking best result
    print(">> Picking best result")
    min_val_loss = None

    for i, checkpoint in enumerate(ckpt_manager.checkpoints):
        ckpt.restore(checkpoint).assert_consumed()

        train_metrics_results = _print_metrics({"train_loss": train_metric})
        val_metrics_results = _print_metrics({"val_loss": val_metric})
        print('Epoch {} - {} - {}'.format(i+1, train_metrics_results, val_metrics_results))
        val_loss_result = val_metric.result()
        if min_val_loss is None or min_val_loss > val_loss_result:
            min_train_loss = train_metric.result()
            min_val_loss = val_loss_result
            min_index = i
    
    ckpt.restore(ckpt_manager.checkpoints[min_index])
    print("best result picked -> epoch: {0} - train_{1}: {2} - val_{1}: {3}".format(min_index + 1, loss_fn_name,
        ('%.4f' if min_train_loss > 1e-3 else '%.4e') % min_train_loss,
        ('%.4f' if min_val_loss > 1e-3 else '%.4e') % min_val_loss))

    # Saving model's weights
    print(">> Saving model's weights")
    dest_path = paths["weights"] + w_filename_template % (encoder, ds_name, loss_fn_name)
    if min_index < 2:
        model.freeze_unfreeze_encoder_trained_layers(False)
    model.save_weights(dest_path)
    print("weights are saved to:\n%s" % dest_path)

def test_model(ds_name, encoder, paths, categorical=False):
    """The main function for executing network testing. It loads the specified
       dataset iterator and optimized saliency model. By default, when no model
       checkpoint is found locally, the pretrained weights will be downloaded.

    Args:
        ds_name (str): Denotes the dataset that was used during training.
        encoder (str): the name of the encoder want to be used to predict.
        paths (dict, str): A dictionary with all path elements.
    """

    w_filename_template = "/%s_%s_%s_weights.h5" # [encoder]_[ds_name]_weights.h5

    (test_ds, n_test) = data.load_test_dataset(ds_name, paths["data"], categorical)
    
    print(">> Preparing model with encoder %s..." % encoder)

    model = MyModel(encoder, ds_name, "test")

    weights_path = paths["weights"] + w_filename_template % (encoder, ds_name, loss_fn_name)
    if os.path.exists(weights_path):
        print("Weights are loaded!\n    %s"%weights_path)
    else:
        download.download_pretrained_weights(paths["weights"], encoder, ds_name, loss_fn_name)
    model.load_weights(weights_path)
    del weights_path

    print(">> Start predicting using model trained on %s..." % ds_name.upper())
    results_path = paths["results"] + "%s/%s/%s/" % (ds_name, encoder, loss_fn_name)

    # Preparing progbar
    test_progbar = Progbar(n_test)
    for test_images, test_ori_sizes, test_filenames in test_ds:
        pred = test_step(test_images, model)
        for pred, filename, ori_size in zip(pred, test_filenames.numpy(), test_ori_sizes):
            img = data.postprocess_saliency_map(pred, ori_size, as_image=True)
            tf.io.write_file(results_path + filename.decode("utf-8"), img)
        test_progbar.add(test_images.shape[0])

def eval_results(ds_name, encoder, paths):
    """The main function for executing network training. It loads the specified
       dataset iterator, saliency model, and helper classes. Training is then
       performed in a new session by iterating over all batches for a number of
       epochs. After validation on an independent set, the model is saved and
       the training history is updated.

    Args:
        ds_name (str): Denotes the dataset to be used during training.
        paths (dict, str): A dictionary with all path elements.
    """

    w_filename_template = "/%s_%s_%s_weights.h5" # [encoder]_[ds_name]_[loss_fn_name]_weights.h5

    (eval_ds, n_eval) = data.load_eval_dataset(ds_name, paths["data"])
    
    print(">> Preparing model with encoder %s..." % encoder)

    model = MyModel(encoder, ds_name, "train")

    if "trained_weights" in paths:
        if os.path.exists(paths["trained_weights"]):
            weights_path = paths["trained_weights"]
        else:
            raise ValueError("could not find the specified weights file.\n    specified weights: %s"%paths["trained_weights"])
    else:
        weights_path = paths["weights"] + w_filename_template % (encoder, ds_name, loss_fn_name)

    if os.path.exists(weights_path):
        print("Weights are loaded!\n    %s"%weights_path)
    else:
        download.download_pretrained_weights(paths["weights"], encoder, "salicon", loss_fn_name)
    
    model.load_weights(weights_path)
    del weights_path

    model.summary()

    # Preparing
    metrics = config.PARAMS["metrics"]

    print("\n>> Start evaluating model on %s..." % ds_name.upper())
    print(("Evaluation details:" +
    "\n{0:<4}Metrics: {2}").format(" ", loss_fn_name, ", ".join(metrics), **config.PARAMS))
    print("_" * 65)

    eval_progbar = Progbar(n_eval)
    categorical = config.SPECS[ds_name].get("categorical", False)
    cat_metrics = {}
    for eval_x, eval_fixs, eval_y_true, eval_ori_sizes, eval_filenames in eval_ds:
        eval_y_pred = test_step(eval_x, model)
        for pred, y_true, fixs, filename, ori_size in zip(eval_y_pred, eval_fixs, eval_y_true, eval_filenames.numpy(), eval_ori_sizes):
            pred = tf.expand_dims(data.postprocess_saliency_map(pred, ori_size), axis=0)
            fixs = tf.expand_dims(fixs, axis=0)
            y_true = tf.expand_dims(y_true, axis=0)

            met_vals = _calc_metrics(metrics, y_true, fixs, pred)
            
            if categorical:
                cat = "/".join(filename.decode("utf-8").split("/")[:-1])
                if not cat in cat_metrics:
                    cat_metrics[cat] = {}
                    for name in metrics:
                        cat_metrics[cat][name] = {"sum":0, "count": 0}
                for name, value in met_vals.items():
                    cat_metrics[cat][name]["sum"] += value
                    cat_metrics[cat][name]["count"] += 1
        eval_progbar.add(eval_x.shape[0], met_vals.items())

    for cat, cat_met in cat_metrics.items():
        to_print = []
        for name, value in cat_met.items():
            _mean = value["sum"]/value["count"]
            to_print.append("{}: {}".format(name, ('%.4f' if _mean > 1e-3 else '%.4e') % _mean))
        print('Results ({}): {}'.format(cat, " - ".join(to_print)))



def find_n_high(ds_name, encoder, paths, n, metric, negate=False):
    """The main function for executing network training. It loads the specified
       dataset iterator, saliency model, and helper classes. Training is then
       performed in a new session by iterating over all batches for a number of
       epochs. After validation on an independent set, the model is saved and
       the training history is updated.

    Args:
        ds_name (str): Denotes the dataset to be used during training.
        paths (dict, str): A dictionary with all path elements.
    """

    w_filename_template = "/%s_%s_%s_weights.h5" # [encoder]_[ds_name]_[loss_fn_name]_weights.h5
    
    (eval_ds, n_eval) = data.load_eval_dataset(ds_name, paths["data"])
    
    print(">> Preparing model with encoder %s..." % encoder)

    model = MyModel(encoder, ds_name, "train")

    if "trained_weights" in paths:
        if os.path.exists(paths["trained_weights"]):
            weights_path = paths["trained_weights"]
        else:
            raise ValueError("could not find the specified weights file.\n    specified weights: %s"%paths["trained_weights"])
    else:
        weights_path = paths["weights"] + w_filename_template % (encoder, ds_name, loss_fn_name)

    if os.path.exists(weights_path):
        print("Weights are loaded!\n    %s"%weights_path)
    else:
        download.download_pretrained_weights(paths["weights"], encoder, "salicon", loss_fn_name)
    
    model.load_weights(weights_path)
    del weights_path

    model.summary()

    # Preparing

    print("\n>> Start finding %d %s results for model on %s..." % (n, "worst" if negate else "best",ds_name.upper()))
    print(("Evaluation details:" +
        "\n{0:<4}Metric: {1}").format(" ", metric))
    print("_" * 65)

    eval_progbar = Progbar(n_eval)
    min_heap = []
    count = 0
    sign = -1 if negate else 1
    for eval_x, eval_fixs, eval_y_true, eval_ori_sizes, eval_filenames in eval_ds:
        eval_y_pred = test_step(eval_x, model)
        for pred, y_true, fixs, filename, ori_size in zip(eval_y_pred, eval_fixs, eval_y_true, eval_filenames.numpy(), eval_ori_sizes):
            pred = tf.expand_dims(data.postprocess_saliency_map(pred, ori_size), axis=0)
            fixs = tf.expand_dims(fixs, axis=0)
            y_true = tf.expand_dims(y_true, axis=0)

            score = _calc_metrics([metric], y_true, fixs, pred)[metric].numpy() * sign
            
            if count < n:
                count+=1
                heapq.heappush(min_heap, (score, filename.decode("utf-8")))
            else:
                heapq.heappushpop(min_heap, (score, filename.decode("utf-8")))
        eval_progbar.add(eval_x.shape[0])
    
    min_heap.sort(reverse=True)
    for s, n in min_heap:
        print(s, n)

def main():
    """The main function reads the command line arguments, invokes the
       creation of appropriate path variables, and starts the training
       or testing procedure for a model.
    """

    current_path = os.path.dirname(os.path.realpath(__file__))
    default_data_path = current_path + "/data"

    datasets_list = list(config.SPECS.keys())
    encoders_list = ["atrous_resnet", "atrous_xception", "ml_atrous_vgg"]
    commands_dict = {"train":{"help": "train the model",
                              "args": ["path"]},
                     "test":{"help": "predict saliency maps using the model",
                             "args": ["path", "categorical"]},
                     "summary":{"help": "show summary of the model", 
                                "args": ["deep"]},
                     "eval":{"help": "eval predict saliency maps predicted by the model",
                             "args": ["path", "weights"]},
                     "find_n_high": {"help": "find n best/worst predictions",
                             "args": ["path", "weights", "number", "negate", "metric"]
                     }}


    args_opts = {
        "data": {
            "args": ("-d", "--data"),
            "kwargs": {
                "metavar": "DATA", "choices": datasets_list, "default": datasets_list[0],
                "help": "define which dataset the model will be trained on or which dataset weights to use for testing"}},
        "encoder": {
            "args": ("-e", "--encoder"),
            "kwargs": {
                "metavar": "ENCODER", "choices": encoders_list, "default": encoders_list[0],
                "help": "specify the encoder (available: %s)" % " or ".join(encoders_list)}},
        "path": {
            "args": ("-p", "--path"),
            "kwargs": {
                "metavar": "DATA_PATH", "default": default_data_path,
                "help":"specify the path where training data will be downloaded to or test data is stored"}},
        "categorical": {
            "args": ("-c", "--categorical"),
            "kwargs": {
                "action":"store_true",
                "help":"specify wether the test data is categorical or not."}},
        "deep": {
            "args": ("-D", "--deep"),
            "kwargs": {
                "action":"store_true",
                "help":"specify wether the summary of nested model would like to be shown as well."}},
        "weights": {
            "args": ("-w", "--weights"),
            "kwargs": {
                "metavar": "WEIGHTS",
                "help": "specify explicitly path to weights file."}},
        "limit-threads": {
            "args": ("-L", "--threads-limit"),
            "kwargs": {
                "metavar": "THREADS_LIMIT", "type": int, "default": 0,
                "help": "speficiy wether to limit the thread to 1 or not"}},
        "number": {
            "args": ("-n", "--number"),
            "kwargs": {
                "metavar": "NUMBER", "type": int, "required": True,
                "help": "speficiy the number of highest score"}},
        "negate": {
            "args": ("-N", "--negate"),
            "kwargs": {
                "action":"store_true",
                "help":"negate, e.g. find n lowest for find_n_high."}},
        "metric": {
            "args": ("-m", "--metric"),
            "kwargs": {
                "metavar": "METRIC", "choices": config.MET_SPECS.keys(), "required": True,
                "help": "speficiy the metric (available: %s)" % " or ".join(encoders_list)}}
        }

    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(*args_opts["limit-threads"]["args"], **args_opts["limit-threads"]["kwargs"])
    parser.add_argument(*args_opts["encoder"]["args"], **args_opts["encoder"]["kwargs"])
    parser.add_argument(*args_opts["data"]["args"], **args_opts["data"]["kwargs"])
    subparsers = parser.add_subparsers( dest="action", metavar="ACTION",
                                        required=True, parser_class=argparse.ArgumentParser)
    
    for cmd, cmd_opt in commands_dict.items():
        sub_command_parser = subparsers.add_parser(cmd, help=cmd_opt["help"])
        for cmd_arg in cmd_opt["args"]:
            sub_command_parser.add_argument(*args_opts[cmd_arg]["args"], **args_opts[cmd_arg]["kwargs"])

    args = parser.parse_args()
    
    encoder_name = args.encoder
    action = args.action

    if args.threads_limit > 0:
        tf.config.threading.set_intra_op_parallelism_threads(args.threads_limit)
        tf.config.threading.set_inter_op_parallelism_threads(args.threads_limit)

    if action == "train":
        train_model(args.data, encoder_name, define_paths(current_path, args))
    elif action == "test":
        test_model(args.data, encoder_name, define_paths(current_path, args), args.categorical)
    elif action == "summary":
        model = MyModel(encoder_name, args.data, "test")
        model.summary()
        if args.deep:
            for layer in model.layers:
                if type(layer).__name__ == "Model":
                    layer.summary()

    elif action == "eval":
        eval_results(args.data, encoder_name, define_paths(current_path, args))
    elif action == "find_n_high":
        find_n_high(args.data, encoder_name,
                        define_paths(current_path, args), args.number, args.metric, args.negate)


if __name__ == "__main__":
    main()
