# Creating a Redis deployment

## Setup a replicated single shard server

This will deploy a single shard that is replicated with a primary for write and read spread across all the replicas.

### (1) Create the kustomization directory

Setup the kustomize directory (e.g., called `index`) with a application suffix (e.g., `app`)
```
DIR=index
NAMESPACE=data
SUFFIX=app
mkdir -p $DIR
cat <<EOF > ${DIR}/kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: ${NAMESPACE}
nameSuffix: -${SUFFIX}
bases:
- ../single
resources:
- partition-index.yaml
commonLabels:
  server: ${SUFFIX}-index
secretGenerator:
  - name: redis-etc
    files:
    - users.acl
EOF
```

Generate the users for Redis (`default` is required):

```
ADMIN=`uuidgen`
WORKLOAD=`uuidgen`
cat <<EOF > ${DIR}/users.acl
user default on +@all ~* >${ADMIN}
user workload on +@all -@dangerous +info ~* >${WORKLOAD}
EOF
```

### (2) Configure the partition

The number of replicas will control the failover behavior in case of node
termination. Within a partition, no two Redis service pods can run on the
same node.

```
MEMORY=64Gi
STORAGE=128Gi
STORAGE_CLASS=standard-rwo
REPLICAS=2
PARTITION=index; python gen.py single-partition.yaml partition=${PARTITION} replicas=${REPLICAS} memory=${MEMORY} storageClassName=${STORAGE_CLASS} storage=${STORAGE} > ${DIR}/partition-index.yaml
```

**Note:** The choice of `STORAGE_CLASS` value depends on the Kubernetes cluster or manage service for Kubernetes you are using.

### (3) Deploy the configuration

Apply the configuration

```
kubectl apply -k ${DIR}
```

### (4) Replicate the shard

In the StatefulSet, the first pod will be suffixed with `-0` and we'll use this
as the primary shard. All the pods get an internal DNS name and so we can replicate
read-only replicas against the primary.

We can only write to the primary shard.

```
ADMIN=`grep default index/users.acl | awk '{print $6}' | sed 's/^>//'`
PARTITION=index; kubectl exec pod/redis-partition-${PARTITION}-${SUFFIX}-1 -- redis-cli -a ${ADMIN} replicaof redis-partition-${PARTITION}-${SUFFIX}-0.redis-${SUFFIX}.data.svc.cluster.local 6379; kubectl exec pod/redis-partition-${PARTITION}-${SUFFIX}-1 -- redis-cli -a ${ADMIN} config set masterauth ${ADMIN};
```

Then we label the primary pod:

```
PARTITION=index; kubectl label pod/redis-partition-${PARTITION}-${SUFFIX}-0 shard-role=primary
```

We can now write to the primary:

```
kubectl exec service/redis-primary-${SUFFIX} -- redis-cli -a ${ADMIN} set A 10
```

and read from a replica:

```
kubectl exec pod/redis-partition-${PARTITION}-${SUFFIX}-1 -- redis-cli -a ${ADMIN} get A
```

If the primary goes down, we can promote a replica to master manually but we
would have to change replication of all the remaining replicas.

**Danger Will Robinson!** If the primary shard comes up empty, the replicas
will sync to empty. Thus, it is important that the statefulset does its job.

**Note** The database backup still needs to be pushed to a safe place.

What to keep in mind:

 * the primary label does not persist over pod restarts
 * replication does not persist with this setup. These values would need to be added to the redis.conf of the replica

### Workloads

A workload password can be retrieved via:

```
SECRET=`kubectl get statefulset.apps/redis-partition-${PARTITION}-${SUFFIX} -o jsonpath={.spec.template.spec.volumes} | jq -r '.[] | select(.name | contains("redis-etc")) | .secret.secretName'`
kubectl get secret/${SECRET} -o json | jq -r '.data["users.acl"]' | base64 -d | grep "user workload" | awk '{print $7}' | sed 's/^>//'
```

That password can be used with the `workload` user to login to the database. Similarly,
the administrator password can be retrieved from the same secret.

### Teardown

Delete the statefulset, service, and configmap:

```
kubectl delete statefulset.apps/redis-partition-${PARTITION}-${SUFFIX} service/redis-${SUFFIX} configmap/redis-${SUFFIX}
kubectl get secrets | grep redis-etc | awk '{print $1}' | xargs -I {} kubectl delete secret/{}
```

## Cluster Deployment

This will deploy a cluster with a set of partitions of the keyspace.

### (1) Generate the kustomization directory

Setup the kustomize directory (e.g., called `index`) with a application suffix (e.g., `app`)
```
DIR=cluster-index
NAMESPACE=data
CLUSTER=app
mkdir -p $DIR
cat <<EOF > ${DIR}/kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: ${NAMESPACE}
nameSuffix: -${CLUSTER}
bases:
- ../cluster
resources:
- partition-p1.yaml
- partition-p2.yaml
- partition-p3.yaml
- envoy.yaml
commonLabels:
  server: ${CLUSTER}-index
secretGenerator:
  - name: redis-etc
    files:
    - users.acl
EOF
```

### (2) Configure the partitions

In the 'k8s' directory, create the partition resource configuration:
```
MEMORY=42Gi
STORAGE=84Gi
STORAGE_CLASS=standard-rwo
REPLICAS=2
PARTITION=p1; python ../gen.py cluster-partition.yaml partition=${PARTITION} replicas=${REPLICAS} memory=${MEMORY} storageClassName=${STORAGE_CLASS} storage=${STORAGE} > ${DIR}/partition-p1.yaml
PARTITION=p2; python ../gen.py cluster-partition.yaml partition=${PARTITION} replicas=${REPLICAS} memory=${MEMORY} storageClassName=${STORAGE_CLASS} storage=${STORAGE} > ${DIR}/partition-p2.yaml
PARTITION=p3; python ../gen.py cluster-partition.yaml partition=${PARTITION} replicas=${REPLICAS} memory=${MEMORY} storageClassName=${STORAGE_CLASS} storage=${STORAGE} > ${DIR}/partition-p3.yaml
```

**Note:** The choice of `STORAGE_CLASS` value depends on the Kubernetes cluster or manage service for Kubernetes you are using.

**Note:** The number and size of partitions is a tuning parameter for the database.

### (3) Configure the envoy proxy

Create the envoy config:

```
python gen.py envoy.yaml name=${CLUSTER} namespace=data > ${DIR}/envoy.yaml
```

### (4) Deploy the cluster

Apply the configuration

```
kubectl apply -k ${DIR}
```

### (5) Setup the cluster replication and slots

Now we can see the shard replicas by pods:

```
kubectl get pods -l cluster=${CLUSTER} -l role=partition-shard
```

Introduce the shards to the cluster:

```
PARTITION=p1; CLUSTER_MEMBER=`kubectl get pod/redis-partition-${PARTITION}-${CLUSTER}-0 -o jsonpath='{.status.podIP}'`
PARTITION=p1; kubectl get pods -l cluster=${CLUSTER} -l partition=${PARTITION} -o json | jq -r '.items[].metadata.name' | xargs -I {} kubectl exec pod/{} -- redis-cli cluster meet ${CLUSTER_MEMBER} 6379
PARTITION=p2; kubectl get pods -l cluster=${CLUSTER} -l partition=${PARTITION} -o json | jq -r '.items[].metadata.name' | xargs -I {} kubectl exec pod/{} -- redis-cli cluster meet ${CLUSTER_MEMBER} 6379
PARTITION=p3; kubectl get pods -l cluster=${CLUSTER} -l partition=${PARTITION} -o json | jq -r '.items[].metadata.name' | xargs -I {} kubectl exec pod/{} -- redis-cli cluster meet ${CLUSTER_MEMBER} 6379
```

Replicate the shards:

```
PARTITION=p1; kubectl exec pod/redis-partition-${PARTITION}-${CLUSTER}-0 -- redis-cli cluster myid | xargs -I {} kubectl exec pod/redis-partition-${PARTITION}-${CLUSTER}-1 -- redis-cli cluster replicate {}
PARTITION=p2; kubectl exec pod/redis-partition-${PARTITION}-${CLUSTER}-0 -- redis-cli cluster myid | xargs -I {} kubectl exec pod/redis-partition-${PARTITION}-${CLUSTER}-1 -- redis-cli cluster replicate {}
PARTITION=p3; kubectl exec pod/redis-partition-${PARTITION}-${CLUSTER}-0 -- redis-cli cluster myid | xargs -I {} kubectl exec pod/redis-partition-${PARTITION}-${CLUSTER}-1 -- redis-cli cluster replicate {}
```

Set the cluster slots:

```
PARTITION=p1; kubectl exec pod/redis-partition-${PARTITION}-${CLUSTER}-0 -- redis-cli cluster addslotsrange 0 5461
PARTITION=p2; kubectl exec pod/redis-partition-${PARTITION}-${CLUSTER}-0 -- redis-cli cluster addslotsrange 5462 10922
PARTITION=p3; kubectl exec pod/redis-partition-${PARTITION}-${CLUSTER}-0 -- redis-cli cluster addslotsrange 10923 16383
```

Note: All 16384 cluster slots must be assigned for the cluster to be valid.

### Testing

Let's write some data!

The key `AAA` should be in partition 1
The key `BBB` should be in partition 3
The key `CCC` should be in partition 2

```
kubectl exec -it pod/redis-partition-${CLUSTER}-p1-0 -- redis-cli
127.0.0.1:6379> cluster keyslot AAA
(integer) 3205
127.0.0.1:6379> cluster keyslot BBB
(integer) 12517
127.0.0.1:6379> cluster keyslot CCC
(integer) 9413
127.0.0.1:6379> set AAA 10
OK
127.0.0.1:6379> set BBB 20
(error) MOVED 12517 10.1.236.219:6379
127.0.0.1:6379> set CCC 30
(error) MOVED 9413 10.1.174.13:6379
```

```
kubectl exec -it pod/redis-partition-${CLUSTER}-p1-0 -- redis-cli get AAA
```

Now we need to write BBB/CCC to the right nodes:

```
kubectl exec -it pod/redis-partition-${CLUSTER}-p3-0 -- redis-cli set BBB 20
kubectl exec -it pod/redis-partition-${CLUSTER}-p2-0 -- redis-cli set CCC 30
```

Within the K8s cluster, you can use a cluster-aware client to access the cluster and read and write to the correct shard.

Outside the K8s cluster (or via an ingress), you can just use the envoy proxy. For example, forward
the service:

```
kubectl port-forward service/redis-proxy-${CLUSTER} 6379   
```

Then you can:

```
% redis-cli
127.0.0.1:6379> set AAA 10
OK
127.0.0.1:6379> set BBB 20
OK
127.0.0.1:6379> set CCC 30
OK
127.0.0.1:6379> get AAA
"10"
127.0.0.1:6379> get BBB
"20"
127.0.0.1:6379> get CCC
"30"
127.0.0.1:6379>
```

You cannot use the redis cluster directly outside of the K8s cluster because
the cluster IPs are only valid within the cluster nodes.

### Cluster clients

The envoy proxy does any module commands or cluster management commands. The cluster client
will work within the K8s cluster without further configuration. If the client code is
running outside the K8s cluster, the IP addresses of the nodes must be translated.

This mapping can be configured as:

```python
from rediscluster import RedisCluster

def mapping(ip,port):
  return {'from_host':ip,'from_port':6379,'to_host':'0.0.0.0','to_port':port}

primary_only = {
  63791 : ['10.1.44.41'],
  63792 : ['10.1.171.248'],
  63793 : ['10.1.236.241']
}

all_nodes = {
  63791 : ['10.1.44.41','10.1.174.34'],
  63792 : ['10.1.171.248','10.1.75.247'],
  63793 : ['10.1.236.241','10.1.44.42']
}

def cluster_client(nodes=all_nodes):
  remap = []
  for port, addresses in nodes.items():
    for address in addresses:
      remap.append(mapping(address,port))

  return RedisCluster(
      startup_nodes=[{"host": "0.0.0.0", "port": "63791"}],
      decode_responses=True,
      host_port_remap=remap
  )
```

And ingress or port-forwarding can be used:

```
kubectl port-forward pod/redis-partition-p1-${CLUSTER}-0 63791:6379 &
kubectl port-forward pod/redis-partition-p2-${CLUSTER}-0 63792:6379 &
kubectl port-forward pod/redis-partition-p3-${CLUSTER}-0 63793:6379 &
```
