output "e2e_user_access_key" {
  value = aws_iam_access_key.e2e_test.id
}

output "e2e_user_secret_key" {
  value = aws_iam_access_key.e2e_test.secret
}

output "workload_role_arn" {
  value = aws_iam_role.workload_role.arn
}
