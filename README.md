# Automated JupyterLab Demo Deployment

## Basic Usage

If all you want to do is run an LSST JupyterLab Demo environment, hosted
at Google Kubernetes Engine, whose domain name is hosted in AWS Route
53, with GitHub authentication against a whitelist of allowed GitHub
organizations, the following should get you going.

1. Choose a fully-qualified domain name in a domain that you control and
   that is hosted by AWS Route 53.  The FQDN need not exist, as long as
   you have write access to the domain that contains it at Route 53.

2. Go to GitHub.  Decide what organizations you want to allow as your
   whitelist, and which organization should own the OAuth callback
   (presumably that organization will be in the whitelist).  You must
   have administrative privileges over the organization.
   
    1. Go to that organization's page and click on `Settings`.
    2. Go to `OAuth Apps` under `Developer Settings`.
	3. Click on `New OAuth App`.
	4. The Application Name is probably something to do with
       JupyterLab.  The `Homepage URL` is just `https://` prepended to the
       FQDN you chose above.  The Authorization callback URL is the
       `Homepage URL` prepended to `/hub/oauth_callback`.
	5. Note the Client ID and Client Secret you get.  You will need
       these later.
	   
3. Get TLS certificates for the hostname you provided above.  AWS
   certificates will not work, as you need the TLS private key for the
   JupyterLab setup.  A wildcard certificate for the domain would work
   fine.  I do not think a self-signed certificate will work, because
   the GitHub callback will (correctly) note that the certificate chain
   is untrusted.  Certificates from letsencrypt.org work fine, although
   that will take setup that is not yet part of the automated
   deployment.  Put the following files (in PEM format) in a directory
   on the machine you are running the deployment from:
   
    - TLS Certificate
	- TLS Key
	- TLS Root Chain
	   
4. Make sure that your shell environment is set up to allow `gcloud`,
   `kubectl`, and `aws` to run authenticated.  This will require `gcloud
   init`, `aws configure`, and an installation of the `kubectl`
   component of `gcloud`.

5. Create a Python virtualenv with Python3 as its interpreter.  I like
   to use `virtualenv-wrapper` and `mkvirtualenv`; if you're doing that,
   `mkvirtualenv -p $(which python3)`.  Activate that virtualenv.

6. Change to a working directory you like and clone this repository
   (`git clone https://github.com/lsst-sqre/jld-deploy`).
   
7. `cd jld-deploy`.  Then (making sure you are inside the activated
   virtualenv) `pip install -e .`.
   
8. `cp deploy.yml mydeploy.yml`.  Edit `mydeploy.yml`.  The following
   settings are required:
    - `kubernetes_cluster_name`: choose one that doesn't exist yet.
	- `hostname`: the FQDN from earlier.
	- `tls_cert`, `tls_key`, and `tls_root_chain`.  These correspond to
      the TLS PEM files you got earlier: specify the (local) path to
      them.
	- `github_client_id` and `github_client_secret` from the OAuth
      application you created earlier.
	- `github_organization_whitelist`: each list entry is a GitHub
      organization name that, if the person logging in is a member of,
      login will be allowed to succeed.

   Feel free to customize other settings.  You particularly may want to
   change the volume size, and I strongly recommend precreating your
   `dhparam.pem` file with `openssl dhparam 2048 > dhparam.pem` in the
   same directory as the rest of your TLS files, and then enabling it in
   the deployment YAML.
   
9. Run `deploy-jupyterlabdemo -f /path/to/mydeploy.yml` .

10. After installation completes, browse to the FQDN you created.

11. When you're done and ready to tear down the cluster, run
    `deploy-jupyterlabdemo -f /path/to/mydeploy.yml -u` .

## Running a custom configuration

1. Specify a directory you want the configuration to be built in with
   `deploy-jupyterlabdemo -f /path/to/mydeploy.yml -d
   /path/to/config/directory -c`
   
2. Edit the Kubernetes deployment files under
   `/path/to/config/directory`.  For instance, you may want to change
   the environment variables the JupyterHub component uses to deploy a
   different JupyterLab image, or indeed you may want to change the
   JupyterHub ConfigMap files to change the authentication or spawner
   configuration.
   
3. Deploy with `deploy-jupyterlabdemo -f /path/to/mydeploy.yml -d
   /path/to/config/directory`
   
## Preserving existing clusters and namespaces.

If you do not want to create and destroy a new cluster each time, you
can use the `--existing-cluster` parameter to `deploy-jupyterlabdemo`.
If you have specified `--existing-cluster` you can also use
`--existing-namespace`.  Both of these settings can also be used during
undeployment to leave the cluster (and namespace) at GKE.  If not
specified the cluster and namespace are created during deployment and
destroyed during undeployment.

   
