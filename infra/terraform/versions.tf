terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Backend intentionally omitted in this scaffold. Production would use a
  # versioned, locked remote backend, e.g.:
  # backend "s3" {
  #   bucket         = "usql-tfstate"
  #   key            = "usql/terraform.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "usql-tflock"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = local.common_tags
  }
}
