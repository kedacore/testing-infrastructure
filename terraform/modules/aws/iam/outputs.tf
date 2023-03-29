output "e2e_user_access_key" {
  value = aws_iam_access_key.e2e_test.id
}

output "e2e_user_secret_key" {
  value = aws_iam_access_key.e2e_test.secret
}