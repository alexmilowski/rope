# Creating an example target:

## Setup

```
kubectl create namespace redis
kubens redis
```

```
kubectl apply -f account.yaml
kubectl apply -f config.yaml
```

## Create cluster partitions

6 nodes: three partitions, two replicas
```
python gen.py cluster-partition.yaml name=mycluster partition=p1 replicas=2 memory=1Gi storageClassName=microk8s-hostpath storage=2Gi | kubectl apply -f -
python gen.py cluster-partition.yaml name=mycluster partition=p2 replicas=2 memory=1Gi storageClassName=microk8s-hostpath storage=2Gi | kubectl apply -f -
python gen.py cluster-partition.yaml name=mycluster partition=p3 replicas=2 memory=1Gi storageClassName=microk8s-hostpath storage=2Gi | kubectl apply -f -
```

We now have redis nodes that are not in a cluster but partitioned with replicas on separate nodes.

We can see each replica pod by:

```
kubectl get pods -l cluster=mycluster -l partition=p1
```

## Setup the headless service


```
python gen.py service.yaml name=mycluster | kubectl apply -f
```

## Create the cluster

Introduce each pod to a cluster member:

```
CLUSTER_MEMBER=`kubectl get pod/redis-partition-mycluster-p1-0 -o jsonpath='{.status.podIP}'`
kubectl get pods -l cluster=mycluster -l partition=p1 -o json | jq -r '.items[].metadata.name' | xargs -I {} kubectl exec pod/{} -- redis-cli cluster meet $CLUSTER_MEMBER 6379
kubectl get pods -l cluster=mycluster -l partition=p2 -o json | jq -r '.items[].metadata.name' | xargs -I {} kubectl exec pod/{} -- redis-cli cluster meet $CLUSTER_MEMBER 6379
kubectl get pods -l cluster=mycluster -l partition=p3 -o json | jq -r '.items[].metadata.name' | xargs -I {} kubectl exec pod/{} -- redis-cli cluster meet $CLUSTER_MEMBER 6379
```

Now we have nodes that are all master members of the cluster:

```
kubectl exec pod/redis-partition-mycluster-p1-0 redis-cli cluster nodes
```

At this point, no node has data. We will assign slots to one pod from each cluster (e.g., the pod suffixed with `-0`) for all 16384 slots:

```
kubectl exec pod/redis-partition-mycluster-p1-0 -- redis-cli cluster addslotsrange 0 5461
kubectl exec pod/redis-partition-mycluster-p2-0 -- redis-cli cluster addslotsrange 5462 10922
kubectl exec pod/redis-partition-mycluster-p3-0 -- redis-cli cluster addslotsrange 10923 16383
```

At this point, the cluster state should be okay:

```
kubectl exec pod/redis-partition-mycluster-p2-0 -- redis-cli cluster info
```

Now, we replicate the master with data in each partition:

```
kubectl exec pod/redis-partition-mycluster-p1-0 -- redis-cli cluster myid | xargs -I {} kubectl exec pod/redis-partition-mycluster-p1-1 -- redis-cli cluster replicate {}
kubectl exec pod/redis-partition-mycluster-p2-0 -- redis-cli cluster myid | xargs -I {} kubectl exec pod/redis-partition-mycluster-p2-1 -- redis-cli cluster replicate {}
kubectl exec pod/redis-partition-mycluster-p3-0 -- redis-cli cluster myid | xargs -I {} kubectl exec pod/redis-partition-mycluster-p3-1 -- redis-cli cluster replicate {}      
```

We can check the replicas with:

```
for id in 1 2 3; do kubectl exec pod/redis-partition-mycluster-p${id}-0 -- redis-cli cluster myid | xargs -I {} kubectl exec pod/redis-partition-mycluster-p${id}-0 -- redis-cli cluster replicas {}; done
```

And the cluster slot allocations:

```
kubectl exec pod/redis-partition-mycluster-p1-0 -- redis-cli cluster slots
```

## Testing

Let's write some data!

The key `AAA` should be in partition 1
The key `BBB` should be in partition 3
The key `CCC` should be in partition 2

```
kubectl exec -it pod/redis-partition-mycluster-p1-0 -- redis-cli
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
kubectl exec pod/redis-partition-mycluster-p1-0 -- redis-cli get AAA
```

Now we need to write BBB/CCC to the right nodes:

```
kubectl exec pod/redis-partition-mycluster-p3-0 -- redis-cli set BBB 20
kubectl exec pod/redis-partition-mycluster-p2-0 -- redis-cli set CCC 30
```

## Using Envoy to proxy to the cluster

```
python gen.py envoy-template.yaml name=mycluster namespace=redis > envoy.yaml
kubectl create configmap envoy --from-file=envoy.yaml
python gen.py proxy.yaml name=mycluster | kubectl apply -f -
```
