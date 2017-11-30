#!/usr/bin/env python3
"""This is a wrapper around a JupyterLabDeployment class.  The class,
at the moment, assumes the following:

1) Deployment is to Google Kubernetes Engine.  You have chosen a cluster name
    and optionally a namespace.
2) Your DNS zone for your external endpoint is hosted in Route 53 and you
    have chosen a FQDN for your application.
3) You are running this from an execution context where gcloud, kubectl,
    and aws have all been set up to run authenticated from the command
    line.
4) At least your external endpoint TLS certs are already generated and
    exist on the local filesystem.  If you need certificates for ELK
    stack communication, those must also be present on the local filesystem.
5) You are using GitHub OAuth for your authentication, and you have
    created an OAuth application Client ID, Client Secret, and a client
    callback that is 'https://fqdn.of.jupyterlab.demo/hub/oauth_callback'
6) Either all of this information has been encoded in a YAML file that you
    reference with the -f switch during deployment, or it's in a series of
    environment variables starting with "JLD_", or you enter it at a
    terminal prompt.
    - If you specify a directory for TLS certificates, the
      certificate, key, and root chain files must be named "cert.pem",
      "key.pem", and "chain.pem" respectively.  If you already have a
      DH Params file, it should be called "dhparam.pem" in the same directory.
    - If present in that directory, the ELK certificates must be
      "beats_cert.pem", "beats_key.pem", and
      "beats_ca.pem" for certificate, key, and certificate authority
      respectively.

Obvious future enhancements are to make this work with a wider variety of
Kubernetes and DNS providers.

It is capable of deploying and undeploying a JupyterLab Demo environment,
or of generating a set of Kubernetes configuration files suitable for
editing and then deploying from this tool.
"""
import argparse
import base64
import datetime
import dns.resolver
import fnmatch
import json
import logging
import os
import os.path
import shutil
import subprocess
import string
import tempfile
import time
import yaml
from contextlib import contextmanager
from jinja2 import Template

JUPYTERLAB_REPO_URL = "https://github.com/lsst-sqre/jupyterlabdemo.git"
EXECUTABLES = ["gcloud", "kubectl", "aws"]
DEFAULT_GKE_ZONE = "us-central1-a"
DEFAULT_GKE_MACHINE_TYPE = "n1-standard-2"
DEFAULT_GKE_NODE_COUNT = 2
DEFAULT_VOLUME_SIZE_GB = 20
ENVIRONMENT_NAMESPACE = "JLD_"
REQUIRED_PARAMETER_NAMES = ["kubernetes_cluster_name",
                            "hostname"]
REQUIRED_DEPLOYMENT_PARAMETER_NAMES = REQUIRED_PARAMETER_NAMES + [
    "github_client_id",
    "github_client_secret",
    "github_organization_whitelist",
    "tls_cert",
    "tls_key",
    "tls_root_chain"
]
PARAMETER_NAMES = REQUIRED_DEPLOYMENT_PARAMETER_NAMES + [
    "kubernetes_cluster_namespace",
    "gke_zone",
    "gke_node_count",
    "gke_machine_type",
    "volume_size_gigabytes",
    "session_db_url",
    "shipper_name",
    "rabbitmq_pan_password",
    "rabbitmq_target_host",
    "rabbitmq_target_vhost",
    "firefly_admin_password"]


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

    def __init__(self, yamlfile=None, params=None, directory=None,
                 disable_prepuller=False, existing_cluster=False,
                 existing_namespace=False, config_only=False):
        self._check_executables(EXECUTABLES)
        self.yamlfile = yamlfile
        self.existing_cluster = existing_cluster
        self.existing_namespace = existing_namespace
        self.directory = directory
        self.config_only = config_only
        self.params = params
        if disable_prepuller:
            self.enable_prepuller = False

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
        """We also set the AWS zone id from this: if the hostname is not
        in an AWS hosted zone, we want to fail sooner rather than later.
        """
        logging.info("Checking authentication.")
        cmd = "gcloud info --format yaml".split()
        rc = self._run(cmd, capture=True)
        if rc.returncode == 0:
            gstruct = yaml.load(rc.stdout.decode('utf-8'))
            acct = gstruct["config"]["account"]
            if not acct:
                raise RuntimeError("gcloud not logged in; " +
                                   "try 'gcloud init'")
        self.params["zoneid"] = self._get_aws_zone_id()

    def _get_aws_zone_id(self):
        hostname = self.params["hostname"]
        domain = '.'.join(hostname.split('.')[1:])
        try:
            zp = self._run(["aws", "route53", "list-hosted-zones",
                            "--output", "json"],
                           capture=True)
            zones = json.loads(zp.stdout.decode('utf-8'))
            zlist = zones["HostedZones"]
            for z in zlist:
                if z["Name"] == domain + ".":
                    zonename = z["Id"]
                    zone_components = zonename.split('/')
                    zoneid = zone_components[-1]
                    return zoneid
            raise RuntimeError("No zone found")
        except Exception as e:
            raise RuntimeError(
                "Could not determine AWS zone id for %s: %s" % (domain,
                                                                str(e)))

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
            for p in params:
                if p not in PARAMETER_NAMES:
                    logging.warn("Unknown parameter '%s'!" % p)

    def _empty_param(self, key):
        if key not in self.params or not self.params[key]:
            return True
        return False

    def _any_empty(self, keylist):
        for key in keylist:
            if self._empty_param(key):
                return True
        return False

    def _run(self, args, directory=None, capture=False,
             capture_stderr=False, check=True):
        stdout = None
        stderr = None
        if capture:
            stdout = subprocess.PIPE
        if capture_stderr:
            stderr = subprocess.PIPE
        if not directory:
            directory = self.directory
        with _wd(directory):
            exe = args[0]
            fqexe = self.executables.get(exe)
            if fqexe:
                args[0] = fqexe
            self._logcmd(args)
            rc = subprocess.run(args, check=check, stdout=stdout,
                                stderr=stderr)
            return rc

    def _get_cluster_info(self):
        if self._empty_param('kubernetes_cluster_name'):
            if not self._empty_param('hostname'):
                hname = self.params["hostname"]
                cname = hname.translate({ord('.'): '-'})
                logging.warn("Using default derived cluster name '%s'" %
                             cname)
                self.params["kubernetes_cluster_name"] = cname
            raise ValueError("'kubernetes_cluster_name' must be set, " +
                             "either explicitly or from 'hostname'.")
        if self._empty_param('kubernetes_cluster_namespace'):
            logging.info("Using default cluster namespace 'default'.")
            self.params["kubernetes_cluster_namespace"] = 'default'
        if self._empty_param('gke_zone'):
            logging.info("Using default gke_zone '%s'." % DEFAULT_GKE_ZONE)
            self.params["gke_zone"] = DEFAULT_GKE_ZONE

    def _validate_deployment_params(self):
        self._get_cluster_info()
        if self._any_empty(REQUIRED_PARAMETER_NAMES):
            raise ValueError("All parameters '%s' must be specified!" %
                             str(REQUIRED_PARAMETER_NAMES))
        if self._empty_param('volume_size_gigabytes'):
            logging.warn("Using default volume size: 20GiB")
            self.params["volume_size_gigabytes"] = DEFAULT_VOLUME_SIZE_GB
        if self.params["volume_size_gigabytes"] < 1:
            raise ValueError("Shared volume must be at least 1 GiB!")
        if self._empty_param('gke_machine_type'):
            self.params['gke_machine_type'] = DEFAULT_GKE_MACHINE_TYPE
        if self._empty_param('gke_node_count'):
            self.params['gke_node_count'] = DEFAULT_GKE_NODE_COUNT
        return

    def _normalize_params(self):
        sz = int(self.params['volume_size_gigabytes'])
        self.params['volume_size'] = str(sz) + "Gi"
        if sz > 1:
            nfs_sz = str(int(0.95 * sz)) + "Gi"
        else:
            nfs_sz = "950Mi"
        self.params['nfs_volume_size'] = nfs_sz
        self.params[
            'github_callback_url'] = ("https://%s/hub/oauth_callback" %
                                      self.params['hostname'])
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
                        'log_shipper_name',
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
        if self._empty_param('tls_dhparam'):
            self._check_executables(["openssl"])
            if self._empty_param('dhparam_bits'):
                self.params['dhparam_bits'] = 2048

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
        with _wd(os.path.join(self.directory, "deployment")):
            self._generate_dhparams()
            self._generate_crypto_key()
            matches = {}
            for c in self.components:
                matches[c] = []
                for root, dirnames, filenames in os.walk(c):
                    for fn in fnmatch.filter(filenames, '*.template.yml'):
                        matches[c].append(os.path.join(root, fn))
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
        if self._empty_param('tls_dhparam'):
            bits = self.params['dhparam_bits']
            with _wd(self.directory):
                ossl = self.executables["openssl"]
                cmd = [ossl, "dhparam", str(bits)]
                rc = self._run(cmd, capture=True)
                dhp = rc.stdout.decode('utf-8')
                self.params["dhparams"] = dhp
        else:
            with open(self.params['tls_dhparam'], "r") as f:
                dhp = f.read()
                self.params["dhparams"] = dhp

    def encode_value(self, key):
        """Cache and return base64 representation of parameter value,
        suitable for kubernetes secrets."""
        if _empty(self.b64_cache, key):
            val = self.params[key]
            if type(val) is str:
                val = val.encode('utf-8')
            self.b64_cache[key] = base64.b64encode(val).decode('utf-8')
        return self.b64_cache[key]

    def encode_file(self, key):
        """Cache and return base64 representation of file contents at
        path specified in 'key', suitable for kubernetes secrets."""
        path = self.params[key]
        cp = path + "_contents"
        if _empty(self.b64_cache, cp):
            try:
                with open(path, "r") as f:
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
                          SHIPPER_NAME=p['log_shipper_name'],
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

    def _save_deployment_yml(self):
        tfmt = '%Y-%m-%d-%H-%M-%S-%f-UTC'
        datestr = datetime.datetime.utcnow().strftime(tfmt)
        outf = os.path.join(self.directory, "deploy.%s.yml" % datestr)
        # Use input file if we have it
        if self.yamlfile:
            shutil.copy2(self.yamlfile, outf)
        else:
            ymlstr = "# JupyterLab Demo deployment file\n"
            ymlstr += "# Created at %s\n" % datestr
            cleancopy = self._clean_param_copy()
            ymlstr += yaml.dump(cleancopy, default_flow_style=False)
            with open(outf, "w") as f:
                f.write(ymlstr)

    def _clean_param_copy(self):
        cleancopy = {}
        pathvars = ['tls_cert', 'tls_key', 'tls_root_chain',
                    'beats_cert', 'beats_key', 'beats_ca']
        fullpathvars = set()
        for p in pathvars:
            v = self.params.get(p)
            if v:
                fullpathvars.add(v)
        for k, v in self.params.items():
            if not v:
                continue
            if k == 'dhparams':
                continue
            if k == 'nfs_volume_size' or k == 'volume_size':
                continue
            if k in fullpathvars:
                continue
            cleancopy[k] = v
        return cleancopy

    def _create_resources(self):
        with self.kubecontext():
            self._create_gke_cluster()
            if self.enable_logging:
                self._create_logging_components()
            self._create_fileserver()
            self._create_fs_keepalive()
            if self.enable_prepuller:
                self._create_prepuller()
            self._create_jupyterhub()
            self._create_nginx()
            self._create_dns_record()

    def _create_gke_cluster(self):
        mtype = self.params['gke_machine_type']
        nodes = self.params['gke_node_count']
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
        if namespace != "default" and not self.existing_namespace:
            self._run(["kubectl", "create", "namespace", namespace])

    def _destroy_gke_cluster(self):
        name = self.params['kubernetes_cluster_name']
        namespace = self.params['kubernetes_cluster_namespace']
        if namespace != "default" and not self.existing_namespace:
            rc = self._run(
                ["kubectl", "config", "current-context"], capture=True)
            if rc.stdout:
                context = rc.stdout.decode('utf-8').strip()
                self._run(["kubectl", "config", "set-context", context,
                           "--namespace", "default"])
            self._run(["kubectl", "delete", "namespace", namespace])
        if not self.existing_cluster:
            self._run_gcloud(["-q", "container", "clusters", "delete", name])

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
                  "jld-fileserver-service.yml",
                  "jld-fileserver-deployment.yml"]:
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
                    "Callback did not succeed after %d %ds iterations" %
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
            if not _empty(struct, "spec"):
                return struct["spec"]["clusterIP"]
        return None

    def _get_external_ip(self):
        rc = self._run(["kubectl", "get", "svc", "jld-nginx",
                        "--namespace=%s" %
                        self.params['kubernetes_cluster_namespace'],
                        "-o", "yaml"],
                       check=False,
                       capture=True)
        if rc.stdout:
            struct = yaml.load(rc.stdout.decode('utf-8'))
            if not _empty(struct, "status"):
                st = struct["status"]
                if not _empty(st, "loadBalancer"):
                    lb = st["loadBalancer"]
                    if not _empty(lb, "ingress"):
                        ng = lb["ingress"]
                        return ng[0]["ip"]
        return None

    def _get_pods_for_name(self, depname):
        logging.info("Getting pod names for '%s'." % depname)
        retval = []
        rc = self._run(["kubectl", "get", "pods", "-o", "yaml"], capture=True)
        struct = yaml.load(rc.stdout.decode('utf-8'))
        for pod in struct["items"]:
            name = pod["metadata"]["name"]
            if name.startswith(depname):
                retval.append(name)
        return retval

    def _destroy_fileserver(self):
        logging.info("Destroying fileserver.")
        ns = self.params["kubernetes_cluster_namespace"]
        for c in [["pvc", "jld-fileserver-home"],
                  ["pv", "jld-fileserver-home-%s" % ns],
                  ["service", "jld-fileserver"],
                  ["pvc", "jld-fileserver-physpvc"],
                  ["deployment", "jld-fileserver"],
                  ["storageclass", "fast"]]:
            self._run_kubectl_delete(c)
        self._destroy_pods_with_callback(self._check_fileserver_gone,
                                         "fileserver")

    def _destroy_pods_with_callback(self, callback, poddesc, tries=60):
        logging.info("Waiting for %s pods to exit." % poddesc)
        try:
            self._waitfor(callback=callback, tries=tries)
        except Exception:
            if self.existing_cluster:
                # If we aren't destroying the cluster, then failing to
                #  take down the keepalive pod means we're going to fail.
                # If we are, the cluster teardown means we don't actually
                #  care a lot whether or not the individual deployment
                #  destructions work.
                raise
            logging.warn("All %s pods did not exit.  Continuing." % poddesc)
            return
        logging.warn("All %s pods exited." % poddesc)

    def _create_fs_keepalive(self):
        logging.info("Creating fs-keepalive")
        self._run_kubectl_create(os.path.join(
            self.directory,
            "deployment",
            "fs-keepalive",
            "jld-keepalive-deployment.yml"
        ))

    def _destroy_fs_keepalive(self):
        logging.info("Destroying fs-keepalive")
        self._run_kubectl_delete(["deployment", "jld-keepalive"])
        self._destroy_pods_with_callback(self._check_keepalive_gone,
                                         "keepalive")

    def _check_keepalive_gone(self):
        return self._check_pods_gone("jld-keepalive")

    def _check_fileserver_gone(self):
        return self._check_pods_gone("jld-fileserver")

    def _check_pods_gone(self, name):
        pods = self._get_pods_for_name(name)
        if pods:
            return None
        return True

    def _create_prepuller(self):
        logging.info("Creating prepuller")
        self._run_kubectl_create(os.path.join(
            self.directory,
            "deployment",
            "prepuller",
            "prepuller-daemonset.yml"
        ))

    def _destroy_prepuller(self):
        logging.info("Destroying prepuller")
        self._run_kubectl_delete(["daemonset", "prepuller"])

    def _create_jupyterhub(self):
        logging.info("Creating JupyterHub")
        directory = os.path.join(self.directory, "deployment", "jupyterhub")
        for c in ["jld-hub-service.yml", "jld-hub-physpvc.yml",
                  "jld-hub-secrets.yml"]:
            self._run_kubectl_create(os.path.join(directory, c))
        cfdir = os.path.join(directory, "config")
        cfnm = "jupyterhub_config"
        self._run(['kubectl', 'create', 'configmap', 'jld-hub-config',
                   "--from-file=%s" % os.path.join(cfdir, "%s.py" % cfnm),
                   "--from-file=%s" % os.path.join(cfdir, "%s.d" % cfnm)])
        self._run_kubectl_create(os.path.join(
            directory, "jld-hub-deployment.yml"))

    def _destroy_jupyterhub(self):
        logging.info("Destroying JupyterHub")
        for c in [["deployment", "jld-hub"],
                  ["configmap", "jld-hub-config"],
                  ["secret", "jld-hub"],
                  ["pvc", "jld-hub-physpvc"],
                  ["svc", "jld-hub"]]:
            self._run_kubectl_delete(c)

    def _create_nginx(self):
        logging.info("Creating Nginx")
        directory = os.path.join(self.directory, "deployment", "nginx")
        for c in ["tls-secrets.yml",
                  "nginx-service.yml",
                  "nginx-deployment.yml"]:
            self._run_kubectl_create(os.path.join(directory, c))

    def _destroy_nginx(self):
        logging.info("Destroying Nginx")
        for c in [["deployment", "jld-nginx"],
                  ["svc", "jld-nginx"],
                  ["secret", "tls"]]:
            self._run_kubectl_delete(c)

    def _create_dns_record(self):
        logging.info("Creating DNS record")
        self._change_dns_record("create")

    def _change_dns_record(self, action):
        zoneid = self.params["zoneid"]
        record = {
            "Comment": "JupyterLab Demo %s/%s" % (
                self.params['kubernetes_cluster_name'],
                self.params['kubernetes_cluster_namespace'],
            ),
            "Changes": []
        }
        if action == "create":
            record["Changes"] = self._generate_upsert_dns()
        elif action == "delete":
            record["Changes"] = self._generate_delete_dns()
        else:
            raise RuntimeError("DNS action must be 'create' or 'delete'")
        changeset = os.path.join(self.directory, "rr-changeset.txt")
        with open(changeset, "w") as f:
            json.dump(record, f)
        self._run(["aws", "route53", "change-resource-record-sets",
                   "--hosted-zone-id", zoneid, "--change-batch",
                   "file://%s" % changeset, "--output", "json"])

    def _generate_upsert_dns(self):
        ip = self._waitfor(callback=self._get_external_ip, tries=30)
        return [
            {
                "Action": "UPSERT",
                "ResourceRecordSet": {
                    "Name": self.params["hostname"],
                    "Type": "A",
                    "TTL": 60,
                    "ResourceRecords": [
                        {
                            "Value": ip
                        }
                    ]
                }
            }
        ]

    def _generate_delete_dns(self):
        host = self.params["hostname"]
        answer = dns.resolver.query(host, 'A')
        response = answer.rrset.to_text().split()
        ttl = int(response[1])
        ip = response[4]
        return [
            {
                "Action": "DELETE",
                "ResourceRecordSet": {
                    "Name": host + ".",
                    "Type": "A",
                    "TTL": ttl,
                    "ResourceRecords": [
                        {
                            "Value": ip
                        }
                    ]
                }
            }
        ]

    def _destroy_dns_record(self):
        logging.info("Destroying DNS record")
        try:
            self._change_dns_record("delete")
        except Exception as e:
            logging.warn("Failed to destroy DNS record: %s" % str(e))

    def _destroy_resources(self):
        with self.kubecontext():
            self._switch_to_context(self.params["kubernetes_cluster_name"])
            self._destroy_dns_record()
            self._destroy_nginx()
            self._destroy_jupyterhub()
            self._destroy_prepuller()
            self._destroy_fs_keepalive()
            self._destroy_fileserver()
            self._destroy_logging_components()
            self._destroy_gke_cluster()

    def _run_gcloud(self, args):
        newargs = ["gcloud"] + args + ["--zone=%s" % self.params["gke_zone"]]
        self._run(newargs)

    def _run_kubectl_create(self, filename):
        self._run(['kubectl', 'create', '-f', filename, "--namespace=%s" %
                   self.params["kubernetes_cluster_namespace"]])

    def _run_kubectl_delete(self, component):
        self._run(['kubectl', 'delete'] + component +
                  ["--namespace=%s" % (
                      self.params["kubernetes_cluster_namespace"])],
                  check=False)

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

    def _create_deployment(self):
        d = self.directory
        if d:
            if not os.path.isdir(d):
                os.makedirs(d)
                self.directory = d
            if not os.path.isdir(os.path.join(d, "deployment")):
                self._generate_config()
            if not self.config_only:
                self._create_resources()
                return
        else:
            with tempfile.TemporaryDirectory() as d:
                self.directory = d
                self._generate_config()
                self._create_resources()
            self.directory = None
        hn = self.params['hostname']
        if self.config_only:
            cfgtext = "Configuration for %s generated" % hn
            if self.directory:
                cfgtext += " in %s" % self.directory
            cfgtext += "."
            logging.info(cfgtext)
        else:
            logging.info("Deployment of %s complete." % hn)

    def _generate_config(self):
        with _wd(self.directory):
            self._get_repo()
            self._copy_deployment_files()
            self._substitute_templates()
            self._rename_fileserver_template()
            self._save_deployment_yml()

    def deploy(self):
        """Deploy JupyterLab Demo cluster.
        """
        if not self.yamlfile and not self.params:
            errstr = "YAML file or parameter set required."
            raise ValueError(errstr)
        self._set_params()
        self._validate_deployment_params()
        self._normalize_params()
        self._create_deployment()
        logging.info("Finished.")

    def undeploy(self):
        """Remove JupyterLab Demo cluster.
        """
        self._set_params()
        self.directory = os.getenv("TMPDIR") or "/tmp"
        self._get_cluster_info()
        self._destroy_resources()
        hn = self.params['hostname']
        logging.info("Removal of %s complete." % hn)


def get_cli_options():
    """Parse command-line arguments"""
    desc = "Deploy or destroy the JupyterLab Demo environment. "
    desc += ("Parameters required in order to be able to destroy the " +
             "JupyterLab Demo are: %s. " % REQUIRED_PARAMETER_NAMES +
             "In order to deploy the cluster, the " +
             "required set is: %s. " % REQUIRED_DEPLOYMENT_PARAMETER_NAMES +
             "These may be set in the YAML file specified with the " +
             "'--file' argument, or passed in in the environment (for each " +
             "name, the corresponding environment variable is 'JLD_' " +
             "prepended to the parameter name in uppercase). If no file " +
             "is specified and a required value is still missing, the " +
             "value will be prompted for on standard input. ")
    desc += ("All deployment parameters may be set from the environment, " +
             "not just required ones. The complete set of recognized " +
             "parameters is: %s. " % PARAMETER_NAMES)
    desc += ("Therefore the set of allowable environment variables is: " +
             "%s." % ["JLD_" + x.upper() for x in PARAMETER_NAMES])
    parser = argparse.ArgumentParser(description=desc)
    parser.add_argument("-c", "--create-config", "--create-configuration",
                        help=("Create configuration only.  Do not deploy." +
                              "  Requires --directory."), action='store_true')
    parser.add_argument("-d", "--directory",
                        help=("Use specified directory and leave " +
                              "configuration files in place.  If " +
                              "directory already contains configuration " +
                              "files, use them instead of cloning " +
                              "repository and resubstituting."),
                        default=None)
    parser.add_argument("-f", "--file", "--input-file",
                        help=("YAML file specifying demo parameters.  " +
                              "Respected for undeployment as well.  If " +
                              "present, used instead of environment or " +
                              "prompt."),
                        default=None)
    parser.add_argument("-u", "--undeploy", "--destroy", "--remove",
                        help="Undeploy JupyterLab Demo cluster.",
                        action='store_true')
    parser.add_argument("--disable-prepuller", "--no-prepuller",
                        help="Do not deploy prepuller",
                        action='store_true')
    parser.add_argument(
        "--existing-cluster", help=("Do not create/destroy cluster.  " +
                                    "Respected for undeployment as well."),
        action='store_true')
    parser.add_argument(
        "--existing-namespace", help=("Do not create/destroy namespace.  " +
                                      "Respected for undeployment as well." +
                                      "  Requires --existing-cluster."),
        action='store_true')
    result = parser.parse_args()
    dtype = "deploy"
    if "undeploy" in result and result.undeploy:
        dtype = "undeploy"
    if "file" not in result or not result.file:
        result.params = get_options_from_environment()
        complete = True
        req_ps = REQUIRED_PARAMETER_NAMES
        if dtype == "deploy":
            req_ps = REQUIRED_DEPLOYMENT_PARAMETER_NAMES
        for n in req_ps:
            if _empty(result, n):
                complete = False
                break
        if not complete:
            result.params = get_options_from_user(dtype=dtype,
                                                  params=result.params)
        result.params = _canonicalize_result_params(result.params)
    return result


def get_options_from_environment(dtype="deploy"):
    retval = {}
    for n in PARAMETER_NAMES:
        e = os.getenv(ENVIRONMENT_NAMESPACE + n.upper())
        if e:
            retval[n] = e
    if _empty(retval, "tls_cert"):
        e = os.getenv(ENVIRONMENT_NAMESPACE + "CERTIFICATE_DIRECTORY")
        if e:
            do_beats = _empty(retval, "beats_cert")
            retval.update(_set_certs_from_dir(e, beats=do_beats))
    return retval


def _set_certs_from_dir(d, beats=False):
    retval = {}
    retval["tls_cert"] = os.path.join(d, "cert.pem")
    retval["tls_key"] = os.path.join(d, "key.pem")
    retval["tls_root_chain"] = os.path.join(d, "chain.pem")
    dhfile = os.path.join(d, "dhparam.pem")
    if os.path.exists(dhfile):
        retval["tls_dhparam"] = dhfile
    if beats:
        beats_cert = os.path.join(d, "beats_cert.pem")
        if os.path.exists(beats_cert):
            retval["beats_cert"] = beats_cert
            retval["beats_ca"] = os.path.join(d, "beats_ca.pem")
            retval["beats_key"] = os.path.join(d, "beats_key.pem")
    return retval


def _canonicalize_result_params(params):
    wlname = "github_organization_whitelist"
    if not _empty(params, wlname):
        params[wlname] = params[wlname].split(',')
    for intval in ["gke_node_count", "volume_size_gigabytes"]:
        if not _empty(params, intval):
            params[intval] = int(params[intval])
    return params


def get_options_from_user(dtype="deploy", params={}):
    prompts = {"kubernetes_cluster_name": "Kubernetes Cluster Name",
               "hostname": "JupyterLab Demo hostname (FQDN)",
               "github_client_id": "GitHub OAuth Client ID",
               "github_client_secret": "GitHub OAuth Client Secret",
               "github_organization_whitelist": "GitHub Organization Whitelist"
               }
    params.update(_get_values_from_prompt(params, ['hostname'], prompts))
    if _empty(params, "kubernetes_cluster_name"):
        hname = params['hostname']
        cname = hname.translate({ord('.'): '-'})
        params["kubernetes_cluster_name"] = cname
        logging.warn("Using derived cluster name '%s'." % cname)
    if dtype == "deploy":
        if _empty(params, "tls_cert"):
            line = ""
            while not line:
                line = input("TLS Certificate Directory: ")
            params.update(_set_certs_from_dir(line))
        params.update(_get_values_from_prompt(
            params, REQUIRED_DEPLOYMENT_PARAMETER_NAMES, prompts))
    return params


def _get_values_from_prompt(params, namelist, prompts={}):
    for n in namelist:
        if _empty(params, n):
            line = ""
            while not line:
                pr = prompts.get(n) or n
                line = input(pr + ": ")
            params[n] = line
    return params


def params_complete(inputdict):
    return False


@contextmanager
def _wd(newdir):
    """Save and restore working directory.
    """
    cwd = os.getcwd()
    os.chdir(newdir)
    yield
    os.chdir(cwd)


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
        ospath = os.getenv("PATH")
        for path in ospath.split(os.pathsep):
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
    e_n = options.existing_namespace
    e_d = options.directory
    c_c = options.create_config
    p_p = options.params
    deployment = JupyterLabDeployment(yamlfile=y_f,
                                      disable_prepuller=d_p,
                                      existing_cluster=e_c,
                                      existing_namespace=e_n,
                                      directory=e_d,
                                      config_only=c_c,
                                      params=p_p
                                      )
    deployment.deploy()


def standalone_undeploy(options):
    """Entrypoint for running undeployment as an executable.
    """
    y_f = options.file
    e_c = options.existing_cluster
    e_n = options.existing_namespace
    p_p = options.params
    deployment = JupyterLabDeployment(yamlfile=y_f,
                                      existing_cluster=e_c,
                                      existing_namespace=e_n,
                                      params=p_p
                                      )
    deployment.undeploy()


def standalone():
    logging.basicConfig(format='%(levelname)s %(asctime)s |%(message)s',
                        datefmt='%m/%d/%Y %I:%M:%S %p',
                        level=logging.DEBUG)
    try:
        import readline  # NoQA
    except ImportError:
        logging.warn("No readline library found; no elaborate input editing.")
    options = get_cli_options()
    if options.undeploy:
        standalone_undeploy(options)
    else:
        standalone_deploy(options)


if __name__ == "__main__":
    standalone()
