#!/bin/bash
# Example script to run at first boot via Openstack
# using the user_data and cloud-init.
# This example installs Ansible and deploys your 
# org's example App.

echo "userdata running on hostname: $(uname -n)"

sudo apt install ansible 2>&1

echo "Cloning repo with example code"
git clone https://github.com/openstacker/nova_anti_affinity.git /tmp/app

pushd /tmp/app
ansible-playbook ./my-app.yml
popd
exit 0
