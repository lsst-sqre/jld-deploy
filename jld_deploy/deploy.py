#!/usr/bin/env python3
import argparse
import base64
import fnmatch
import logging
import os
import os.path
import shutil
import subprocess
import tempfile
import yaml
from jinja2 import Template


JUPYTERLAB_REPO_URL = "https://github.com/lsst-sqre/jupyterlabdemo.git"
EXECUTABLES = ["gcloud", "kubectl", "openssl"]


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
    b64_cache = {}
    executables = {}

    def __init__(self, yamlfile=None, disable_prepuller=False):
        self._check_executables(EXECUTABLES)
        self.yamlfile = yamlfile
        if disable_prepuller:
            self.enable_prepuller = False

    def _check_executables(self, proglist):
        for p in proglist:
            rc = _which(p)
            if not rc:
                raise ValueError("%s not on search path!" % p)
            self.executables[p] = rc

    def _set_params(self):
        with open(self.yamlfile, 'r') as f:
            self.params = yaml.load(f)

    def _empty_param(self, key):
        if key not in self.params or not self.params[key]:
            return True
        return False

    def _any_empty(self, keylist):
        for key in keylist:
            if self._empty_param(key):
                return True
        return False

    def _run(self, args, directory=None):
        if not directory:
            directory = self.directory
        cwd = os.getcwd()
        os.chdir(directory)
        exe = args[0]
        fqexe = self.executables.get(exe)
        if fqexe:
            args[0] = fqexe
        self._logcmd(args)
        subprocess.run(args, check=True)
        os.chdir(cwd)

    def _get_cluster_info(self):
        if self._empty_param('kubernetes_cluster_name'):
            raise ValueError("'kubernetes_cluster_name' must be set.")
        if self._empty_param('kubernetes_cluster_namespace'):
            self.params["kubernetes_cluster_namespace"] = 'default'

    def _validate_deployment_params(self):
        self._get_cluster_info()
        req_params = ["hostname", "github_client_id",
                      "github_client_secret",
                      "github_organization_whitelist",
                      "volume_size_gigabytes",
                      "tls_cert", "tls_key", "tls_root_chain"]
        if self._any_empty(req_params):
            raise ValueError("All parameters '%s' must be specified!" %
                             str(req_params))
        if self.params["volume_size_gigabytes"] < 1:
            raise ValueError("Shared volume must be at least 1 GiB!")
        return

    def _normalize_params(self):
        sz = int(self.params['volume_size_gigabytes'])
        self.params['volume_size'] = str(sz) + "Gi"
        if sz > 1:
            nfs_sz = str(int(0.95 * sz)) + "Gi"
        else:
            nfs_sz = "950Mi"
        self.params['nfs_volume_size'] = nfs_sz
        del(self.params['volume_size_gigabytes'])
        self.params['github_callback_url'] = "https://" + \
            self.params['hostname'] + \
            "/hub/oauth_callback"
        self.params["github_organization_whitelist"] = ','.join(
            self.params["github_organization_whitelist"])
        self._check_optional()

    def _check_optional(self):
        # We give all of these empty string values to make the
        #  templating logic easier.
        if self._empty_param('firefly_admin_password'):
            self.params['firefly_admin_password'] = ''
        else:
            self.enable_firefly = True
        logging_vars = ['rabbitmq_pan_password',
                        'rabbitmq_target_host',
                        'rabbitmq_target_vhost',
                        'shipper_name',
                        'beats_key',
                        'beats_ca',
                        'beats_cert']
        if self._any_empty(logging_vars):
            for l in logging_vars:
                self.params[l] = ''
        else:
            self.enable_logging = True
        if self._empty_param('session_db_url'):
            self.params[
                'session_db_url'] = 'sqlite:////home/jupyter/jupyterhub.sqlite'

    def _get_repo(self):
        if not self.repo_url:
            self.repo_url = JUPYTERLAB_REPO_URL
        self._run(["git", "clone", self.repo_url])

    def _copy_deployment_files(self):
        d = self.directory
        os.mkdir(os.path.join(d, "deployment"))
        repo = (self.repo_url.split('/')[-1])[:-4]
        for c in self.components:
            shutil.copytree(os.path.join(d, repo, c, "kubernetes"),
                            os.path.join(d, "deployment", c))
        shutil.rmtree(os.path.join(d, repo))

    def _substitute_templates(self):
        os.chdir(os.path.join(self.directory, "deployment"))
        self._generate_dhparams()
        self._generate_crypto_key()
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

    def _generate_crypto_key(self):
        ck = os.urandom(16).hex() + ";" + os.urandom(16).hex()
        self.params['crypto_key'] = ck

    def _logcmd(self, cmd):
        cmdstr = " ".join(cmd)
        logging.info("About to run '%s'" % cmdstr)

    def _generate_dhparams(self):
        bits = 256  # FIXME
        cwd = os.getcwd()
        os.chdir(self.directory)
        ossl = self.executables["openssl"]
        cmd = [ossl, "dhparam", str(bits)]
        self._logcmd(cmd)
        dhp = subprocess.check_output(cmd)
        dhp_txt = dhp.decode('utf-8')
        self.params["dhparams"] = dhp_txt
        os.chdir(cwd)

    def encode_value(self, key):
        if _empty(self.b64_cache, key):
            self.b64_cache[key] = base64.b64encode(
                self.params[key].encode('utf-8'))
        return self.b64_cache[key].decode('utf-8')

    def encode_file(self, path):
        cp = path + "_contents"
        if _empty(self.b64_cache, cp):
            try:
                with open(path, "rb") as f:
                    c = f.read()
                    self.params[path] = c
                    b64_c = self.encode_value(path)
                    self.b64_cache[cp] = b64_c
            except IOError:
                self.b64_cache[cp] = ''
        return self.b64_cache[cp]

    def _substitute(self, template):
        destfile = template[:-13] + ".yml"
        p = self.params
        with open(template, 'r') as rf:
            templatetext = rf.read()
            tpl = Template(templatetext)
            # We do not know: NFS_SERVER_IP_ADDRESS so leave it a template.
            out = tpl.render(CLUSTERNAME=p['kubernetes_cluster_name'],
                             GITHUB_CLIENT_ID=self.encode_value(
                                 'github_client_id'),
                             GITHUB_OAUTH_CALLBACK_URL=self.encode_value(
                                 'github_callback_url'),
                             GITHUB_ORGANIZATION_WHITELIST=self.encode_value(
                                 'github_organization_whitelist'),
                             GITHUB_SECRET=self.encode_value(
                                 'github_client_secret'),
                             SESSION_DB_URL=self.encode_value(
                                 'session_db_url'),
                             JUPYTERHUB_CRYPTO_KEY=self.encode_value(
                                 'crypto_key'),
                             CLUSTER_IDENTIFIER=p[
                                 'kubernetes_cluster_namespace'],
                             SHARED_VOLUME_SIZE=p[
                                 'nfs_volume_size'],
                             PHYSICAL_SHARED_VOLUME_SIZE=p[
                                 'volume_size'],
                             ROOT_CHAIN_PEM=self.encode_file('tls_root_chain'),
                             DHPARAM_PEM=self.encode_value("dhparams"),
                             TLS_CRT=self.encode_file('tls_cert'),
                             TLS_KEY=self.encode_file('tls_key'),
                             HOSTNAME=p['hostname'],
                             FIREFLY_ADMIN_PASSWORD=self.encode_value(
                                 'firefly_admin_password'),
                             CA_CERTIFICATE=self.encode_file('beats_ca'),
                             BEATS_CERTIFICATE=self.encode_file('beats_cert'),
                             BEATS_KEY=self.encode_file('beats_key'),
                             SHIPPER_NAME=p['shipper_name'],
                             RABBITMQ_PAN_PASSWORD=self.encode_value(
                                 'rabbitmq_pan_password'),
                             RABBITMQ_TARGET_HOST=p['rabbitmq_target_host'],
                             RABBITMQ_TARGET_VHOST=p['rabbitmq_target_vhost'],
                             NFS_SERVER_IP_ADDRESS='{{NFS_SERVER_IP_ADDRESS}}',
                             )
            with open(destfile, 'w') as wf:
                wf.write(out)
        os.remove(template)

    def _rename_fileserver_template(self):
        # We did not finish substituting the fileserver, because
        #  we need the service address.
        directory = os.path.join(self.directory, "deployment",
                                 "fileserver")
        fnbase = "jld-fileserver-pv"
        src = os.path.join(directory, fnbase + ".yml")
        tgt = os.path.join(directory, fnbase + ".template.yml")
        os.rename(src, tgt)

    def _create_resources(self):
        self._create_gke_cluster()
        import time
        time.sleep(60)

    def _destroy_resources(self):
        self._destroy_gke_cluster()

    def _run_gcloud(self, args):
        zone = "us-central1-a"
        newargs = ["gcloud"] + args + ["--zone=%s" % zone]
        self._run(newargs)

    def _create_gke_cluster(self):
        mtype = "n1-standard-2"
        nodes = 2
        name = self.params['kubernetes_cluster_name']
        self._run_gcloud(["container", "clusters", "create", name,
                          "--num-nodes=%d" % nodes,
                          "--machine-type=%s" % mtype
                          ])
        self._run_gcloud(["container", "clusters", "get-credentials",
                          name])

    def _destroy_gke_cluster(self):
        name = self.params['kubernetes_cluster_name']
        self._run_gcloud(["-q", "container", "clusters", "delete", name])

    def _create_from_template(self):
        with tempfile.TemporaryDirectory() as d:
            logging.info("Working dir: %s" % d)
            self.directory = d
            self._get_repo()
            self._copy_deployment_files()
            self._substitute_templates()
            self._rename_fileserver_template()
            self._create_resources()
        self.directory = None

    def deploy(self):
        if not self.yamlfile:
            raise ValueError("Deployment requires input YAML file!")
        self._set_params()
        self._validate_deployment_params()
        self._normalize_params()
        self._create_from_template()

    def undeploy(self):
        self._set_params()
        self.directory = os.getenv("TMPDIR") or "/tmp"
        self._get_cluster_info()
        self._destroy_resources()


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


def _empty(input_dict, k):
    if k in input_dict and input_dict[k]:
        return False
    return True


def _which(program):
    """https://stackoverflow.com/questions/377017/test-if-executable-exists-in-python/377028#377028
    """
    def is_exe(fpath):
        return os.path.isfile(fpath) and os.access(fpath, os.X_OK)
    fpath, fname = os.path.split(program)
    if fpath:
        if is_exe(program):
            return program
    else:
        for path in os.environ["PATH"].split(os.pathsep):
            exe_file = os.path.join(path, program)
            if is_exe(exe_file):
                return exe_file
    return None


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
    logging.basicConfig(format='%(asctime)s %(message)s',
                        datefmt='%m/%d/%Y %I:%M:%S %p',
                        level=logging.DEBUG)
    options = get_cli_options()
    if options.undeploy:
        standalone_undeploy(options)
    else:
        standalone_deploy(options)
