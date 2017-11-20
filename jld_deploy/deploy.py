#!/usr/bin/env python3
import argparse
import base64
import fnmatch
import os
import shutil
import subprocess
import tempfile
import yaml


class JupyterLabDeployment(object):
    """JupyterLab Deployment object"""
    directory = None
    components = ["logstashrmq", "filebeat", "fileserver", "fs-keepalive",
                  "firefly", "prepuller", "jupyterhub", "nginx"]
    params = None
    repo_url = None
    yamlfile = None
    enable_firefly = False
    enable_prepuller = True
    enable_logging = False

    def __init__(self, yamlfile=None, disable_prepuller=False):
        self.yamlfile = yamlfile
        if disable_prepuller:
            self.enable_prepuller = False

    def _get_cluster_info(self):
        if 'cluster_name' not in self.params or not \
           self.params["cluster_name"]:
            raise ValueError("'cluster_name' must be set.")
        if 'cluster_namespace' not in self.params or not \
           self.params["cluster_namespace"]:
            self.params["cluster_namespace"] = 'default'

    def _set_params(self):
        with open(self.yamlfile, 'r') as f:
            self.params = yaml.load(f)

    def _validate_deployment_params(self):
        self._get_cluster_info()
        for i in ["hostname", "github_client_id", "github_client_secret",
                  "github_organization_whitelist", "volume_size_gigabytes",
                  "tls_cert", "tls_key", "tls_chain"]:
            if i not in self.params or not self.params[i]:
                raise ValueError("Parameter '%s' must be specified!" % i)
        if self.params["volume_size_gigabytes"] < 2:
            raise ValueError("Shared volume must be at least 2 GiB!")
        return

    def _normalize_params(self):
        gbs = int(self.params.volume_size_gigabytes)
        nfs_gbs = int(0.95 * gbs)
        self.params.volume_size_gigabytes = gbs
        self.params.nfs_volume_size_gigabytes = nfs_gbs
        if "firefly_admin_password" in self.params and \
           self.params.firefly_admin_password:
            self.enable_firefly = True
        if "rabbitmq_pan_password" in self.params and \
           self.params.rabbitmq_pan_password:
            self.enable_logging = True

    def _get_repo(self):
        if not self.repo_url:
            self.repo_url = "https://github.com/lsst-sqre/jupyterlabdemo.git"
        cwd = os.getcwd()
        os.chdir(self.directory)
        subprocess.run(["git", "clone", self.repo_url], check=True)
        os.chdir(cwd)

    def _copy_deployment_files(self):
        d = self.directory
        os.mkdir(os.path.join(d, "deployment"))
        repo = (self.repo_url.split('/')[-1])[:-4]
        for c in self.components:
            shutil.copytree(os.path.join(d, repo, c, "kubernetes"),
                            os.path.join(d, "deployment", c))

    def _substitute_templates(self):
        os.chdir(os.path.join(self.directory, "deployment"))
        matches = {}
        for c in self.components:
            matches[c] = []
            for root, dirnames, filenames in os.walk(c):
                for filename in fnmatch.filter(filenames, '*.template.yml'):
                    matches[c].append(os.path.join(root, filename))
        for c in self.components:
            templates = matches[c]
            for t in templates:
                self._substitute(t)

    def _substitute(self, template):
        destfile = template[:-13] + ".yml"
        with open(template, 'r') as rf:
            templatetext = rf.read()
            # Do the thing and put it in outputtext
            outputtext = templatetext
            with open(destfile, 'w') as wf:
                wf.write(outputtext)
        os.remove(template)

    def _create_resources(self):
        import time
        time.sleep(60)

    def _create_from_template(self):
        with tempfile.TemporaryDirectory() as d:
            print("Working dir: %s" % d)
            self.directory = d
            self._get_repo()
            self._copy_deployment_files()
            self._substitute_templates()
            self._create_resources()
        self.directory = None

    def deploy(self):
        if not self.yamlfile:
            raise ValueError("Deployment requires input YAML file!")
        self._set_params()
        self._validate_deployment_params()
        self._create_from_template()

    def undeploy(self):
        self.get_cluster_info()
        pass


def get_cli_options():
    """Parse command-line arguments"""
    parser = argparse.ArgumentParser(description="Specify JupyterLab Demo" +
                                     " parameters.")
    parser.add_argument("-f", "--file", "--input-file",
                        help="YAML file specifying demo parameters",
                        default=None)
    parser.add_argument("-u", "--undeploy",
                        help="Undeploy JupyterLab Demo cluster",
                        action='store_true')
    parser.add_argument("--disable-prepuller", "--no-prepuller",
                        help="Do not deploy prepuller",
                        action='store_true')
    return parser.parse_args()


def encode_value(self, value):
    return base64.b64encode(value)


def encode_file(self, path):
    with open(path, "r") as f:
        return encode_value(f.read())


def standalone_deploy(options):
    """Entrypoint for running deployment as an executable.
    """
    d_p = options.disable_prepuller
    y_f = options.file
    deployment = JupyterLabDeployment(yamlfile=y_f,
                                      disable_prepuller=d_p)
    deployment.deploy()


def standalone_undeploy(options):
    """Entrypoint for running undeployment as an executable.
    """
    y_f = options.file
    deployment = JupyterLabDeployment(yamlfile=y_f)
    deployment.undeploy()


if __name__ == "__main__":
    options = get_cli_options()
    if options.undeploy:
        standalone_undeploy(options)
    else:
        standalone_deploy(options)
