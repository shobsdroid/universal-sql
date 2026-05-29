# EKS cluster + a managed node group with autoscaling bounds. HPA (per-pod
# scaling) lives in the Helm chart; this sets the node floor/ceiling that the
# cluster-autoscaler works within (design doc §14.2). node_max is the cost
# guardrail.

resource "aws_iam_role" "cluster" {
  name = "${var.name_prefix}-eks"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "eks.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role" "nodes" {
  name = "${var.name_prefix}-eks-nodes"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

resource "aws_eks_cluster" "this" {
  name     = var.name_prefix
  role_arn = aws_iam_role.cluster.arn
  version  = "1.30"

  vpc_config {
    subnet_ids              = var.subnet_ids
    endpoint_private_access = true
    # Single-tenant clusters keep the API endpoint private to the tenant VPC.
    endpoint_public_access = !var.single_tenant_vpc
  }
}

resource "aws_eks_node_group" "default" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "${var.name_prefix}-ng"
  node_role_arn   = aws_iam_role.nodes.arn
  subnet_ids      = var.subnet_ids
  instance_types  = ["m6i.xlarge"] # illustrative

  scaling_config {
    desired_size = var.node_min
    min_size     = var.node_min
    max_size     = var.node_max
  }
}
