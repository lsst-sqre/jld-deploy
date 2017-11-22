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
import time
import yaml
from contextlib import contextmanager
from jinja2 import Template


JUPYTERLAB_REPO_URL = "https://github.com/lsst-sqre/jupyterlabdemo.git"
EXECUTABLES = ["gcloud", "kubectl", "openssl"]
DEFAULT_ZONE = "us-central1-a"


class JupyterLabDeployment(object):
    """JupyterLab Deployment object"""
    directory = None
    components = ["logstashrmq", "filebeat", "fileserver", "fs-keepalive",
                  "firefly", "prepuller", "jupyterhub", "nginx"]
    params = None
    repo_url = None
    yamlfile = None
    original_context = None
    enable_firefly = False
    enable_prepuller = True
    enable_logging = False
    existing_cluster = False
    b64_cache = {}
    executables = {}

    def __init__(self, yamlfile=None, params=None, disable_prepuller=False,
                 existing_cluster=False):
        self._check_executables(EXECUTABLES)
        self.yamlfile = yamlfile
        self.existing_cluster = existing_cluster
        if disable_prepuller:
            self.enable_prepuller = False
        if params:
            self.params = params

    @contextmanager
    def kubecontext(self):
        """Save and restore original Kubernetes context.
        """
        self._check_authentication()
        savec = ["kubectl", "config", "current-context"]
        rc = self._run(savec, capture=True, check=False)
        if rc.stdout:
            self.original_context = rc.stdout.decode('utf-8').strip()
        yield
        if self.original_context:
            restorec = ["kubectl", "config",
                        "use-context", self.original_context]
            self._run(restorec, check=False)

    def _check_authentication(self):
        logging.info("Checking authentication.")
        gc = "gcloud container clusters get-credentials"
        checkcmd = {"gke": {"cmd": ["gcloud", "compute", "instances", "list"],
                            "config": "gke init"},
                    "aws": {"cmd": ["aws", "ec2", "describe-instances"],
                            "config": "aws configure"},
                    "kubectl": {"cmd": ["kubectl", "get", "namespaces"],
                                "config": gc}
                    }
        for c in checkcmd:
            cmd = checkcmd[c]["cmd"]
            cfg = checkcmd[c]["config"]
            rc = self._run(cmd, capture=True, check=False)
        if rc.returncode:
            errstr = "%s not correctly configured; try `%s`" % (
                c, cfg)
            raise RuntimeError(errstr)

    def _check_executables(self, proglist):
        for p in proglist:
            rc = _which(p)
            if not rc:
                raise ValueError("%s not on search path!" % p)
            self.executables[p] = rc

    def _set_params(self):
        if not self.params:
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

    def _run(self, args, directory=None, capture=False, check=True):
        stdout = None
        if capture:
            stdout = subprocess.PIPE
        if not directory:
            directory = self.directory
        cwd = os.getcwd()
        os.chdir(directory)
        exe = args[0]
        fqexe = self.executables.get(exe)
        if fqexe:
            args[0] = fqexe
        self._logcmd(args)
        rc = subprocess.run(args, check=check, stdout=stdout)
        os.chdir(cwd)
        return rc

    def _get_cluster_info(self):
        if self._empty_param('kubernetes_cluster_name'):
            raise ValueError("'kubernetes_cluster_name' must be set.")
        if self._empty_param('kubernetes_cluster_namespace'):
            self.params["kubernetes_cluster_namespace"] = 'default'
        if self._empty_param('zone'):
            self.params["zone"] = DEFAULT_ZONE

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
        self.params['github_callback_url'] = \
            "https://%s/hub/oauth_callback" % self.params['hostname']
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
                self._substitute_file(t)

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
        rc = self._run(cmd, capture=True)
        dhp = rc.stdout.decode('utf-8')
        self.params["dhparams"] = dhp
        os.chdir(cwd)

    def encode_value(self, key):
        """Cache and return base64 representation of parameter value,
        suitable for kubernetes secrets."""
        if _empty(self.b64_cache, key):
            self.b64_cache[key] = base64.b64encode(
                self.params[key].encode('utf-8'))
        return self.b64_cache[key].decode('utf-8')

    def encode_file(self, path):
        """Cache and return base64 representation of file contents, suitable
        for kubernetes secrets."""
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

    def _substitute_file(self, templatefile):
        destfile = templatefile[:-13] + ".yml"
        with open(templatefile, 'r') as rf:
            templatetext = rf.read()
            tpl = Template(templatetext)
            out = self._substitute(tpl)
            with open(destfile, 'w') as wf:
                wf.write(out)
        os.remove(templatefile)

    def _substitute(self, tpl):
        """This is the important part.  We just substitute all the values,
        although only a few will be present in any particular input file.
        """
        p = self.params
        # We do not know NFS_SERVER_IP_ADDRESS so leave it a template.
        return tpl.render(CLUSTERNAME=p['kubernetes_cluster_name'],
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
        with self.kubecontext():
            self._create_gke_cluster()
            if self.enable_logging:
                self._create_logging_components()
            self._create_fileserver()
            self.logging("Sleeping for investigation of working dir")
            time.sleep(600)

    def _create_logging_components(self):
        logging.info("Creating logging components.")
        for c in [os.path.join("logstashrmq", "logstashrmq-secrets.yml"),
                  os.path.join("logstashrmq", "logstashrmq-service.yml"),
                  os.path.join("logstashrmq", "logstashrmq-deployment.yml"),
                  os.path.join("filebeat", "filebeat-secrets.yml"),
                  os.path.join("filebeat", "filebeat-daemonset.yml")]:
            self._run_kubectl_create(os.path.join(
                self.directory, "deployment", c))

    def _destroy_logging_components(self):
        logging.info("Destroying logging components.")
        for c in [["daemonset", "filebeat"],
                  ["secret", "filebeat"],
                  ["deployment", "logstash"],
                  ["service", "logstashrmq"],
                  ["secret", "logstashrmq"]]:
            self._run_kubectl_delete(c)

    def _create_fileserver(self):
        logging.info("Creating fileserver.")
        directory = os.path.join(self.directory, "deployment", "fileserver")
        for c in ["jld-fileserver-storageclass.yml",
                  "jld-fileserver-physpvc.yml",
                  "jld-fileserver-service.yml"]:
            self._run_kubectl_create(os.path.join(directory, c))
        ip = self._waitfor(self._get_fileserver_ip)
        ns = self.params["kubernetes_cluster_namespace"]
        self._substitute_fileserver_ip(ip, ns)
        for c in ["jld-fileserver-pv-%s.yml" % ns,
                  "jld-fileserver-pvc.yml"]:
            self._run_kubectl_create(os.path.join(directory, c))

    def _substitute_fileserver_ip(self, ip, ns):
        directory = os.path.join(self.directory, "deployment", "fileserver")
        with open(os.path.join(directory,
                               "jld-fileserver-pv.template.yml"), "r") as fr:
            tmpl = Template(fr.read())
            out = tmpl.render(NFS_SERVER_IP_ADDRESS=ip)
            with open(os.path.join(directory,
                                   "jld-fileserver-pv-%s.yml" % ns),
                      "w") as fw:
                fw.write(out)

    def _waitfor(self, callback, delay=10, tries=10):
        i = 0
        while True:
            i = i + 1
            rc = callback()
            if rc:
                return rc
            logging.info("Waiting %d seconds [%d/%d]." % (delay, i, tries))
            time.sleep(delay)
            if i == tries:
                raise RuntimeError(
                    "Did not receive IP after %d %ds iterations" %
                    (tries, delay))

    def _get_fileserver_ip(self):
        rc = self._run(["kubectl", "get", "svc", "jld-fileserver",
                        "--namespace=%s" %
                        self.params['kubernetes_cluster_namespace'],
                        "-o", "yaml"],
                       check=False,
                       capture=True)
        if rc.stdout:
            struct = yaml.load(rc.stdout.decode('utf-8'))
            if "spec" in struct and "clusterIP" in struct["spec"]:
                if struct["spec"]["clusterIP"]:
                    return struct["spec"]["clusterIP"]
        return None

    def _destroy_fileserver(self):
        logging.info("Destroying fileserver.")
        ns = self.params["kubernetes_cluster_namespace"]
        for c in [["pvc", "jld-fileserver-home"],
                  ["pv", "jld-fileserver-home-%s" % ns],
                  ["service", "jld-fileserver"],
                  ["pvc", "jld-fileserver-physpvc"],
                  ["storageclass", "fast"]]:
            self._run_kubectl_delete(c)

    def _destroy_resources(self):
        with self.kubecontext():
            self._switch_to_context(self.params["kubernetes_cluster_name"])
            self._destroy_logging_components()
            self._destroy_fileserver()
            self._destroy_gke_cluster()

    def _run_gcloud(self, args):
        newargs = ["gcloud"] + args + ["--zone=%s" % self.params["zone"]]
        self._run(newargs)

    def _run_kubectl_create(self, filename):
        self._run(['kubectl', 'create', '-f', filename, "--namespace=%s" %
                   self.params["kubernetes_cluster_namespace"]])

    def _run_kubectl_delete(self, component):
        self._run(['kubectl', 'delete'] + component +
                  ["--namespace=%s" % (
                      self.params["kubernetes_cluster_namespace"])],
                  check=False)

    def _create_gke_cluster(self):
        mtype = "n1-standard-2"
        nodes = 2
        name = self.params['kubernetes_cluster_name']
        namespace = self.params['kubernetes_cluster_namespace']
        if not self.existing_cluster:
            self._run_gcloud(["container", "clusters", "create", name,
                              "--num-nodes=%d" % nodes,
                              "--machine-type=%s" % mtype
                              ])
            self._run_gcloud(["container", "clusters", "get-credentials",
                              name])
        self._switch_to_context(name)
        self._run(["kubectl", "create", "namespace", namespace])

    def _switch_to_context(self, name):
        context = None
        rc = self._run(["kubectl", "config", "get-contexts"], capture=True)
        if rc.stdout:
            lines = rc.stdout.decode('utf-8').split('\n')
            for l in lines:
                w = l.split()
                t_context = w[0]
                if t_context == '*':
                    t_context = w[1]
                if t_context.endswith(name):
                    context = t_context
                    break
        if not context:
            raise RuntimeError(
                "Could not find context for cluster '%s'" % name)
        self._run(["kubectl", "config", "use-context", context])
        self._run(["kubectl", "config", "set-context", context,
                   "--namespace", self.params['kubernetes_cluster_namespace']])

    def _destroy_gke_cluster(self):
        name = self.params['kubernetes_cluster_name']
        namespace = self.params['kubernetes_cluster_namespace']
        if namespace != "default":
            rc = self._run(
                ["kubectl", "config", "current-context"], capture=True)
            if rc.stdout:
                context = rc.stdout.decode('utf-8').strip()
                self._run(["kubectl", "config", "set-context", context,
                           "--namespace", "default"])
            self._run(["kubectl", "delete", "namespace", namespace])
        if not self.existing_cluster:
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
    parser.add_argument(
        "--existing-cluster", help="Do not create/destroy cluster",
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
    e_c = options.existing_cluster
    deployment = JupyterLabDeployment(yamlfile=y_f,
                                      disable_prepuller=d_p,
                                      existing_cluster=e_c,
                                      )
    deployment.deploy()


def standalone_undeploy(options):
    """Entrypoint for running undeployment as an executable.
    """
    y_f = options.file
    e_c = options.existing_cluster
    deployment = JupyterLabDeployment(yamlfile=y_f,
                                      existing_cluster=e_c,
                                      )
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
