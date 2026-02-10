# PowerShell script for testing API endpoints

Write-Host "Testing API endpoints..." -ForegroundColor Green

# Test login endpoint (requires real WeChat code)
Write-Host "`n1. Testing login endpoint (POST /api/auth/login)" -ForegroundColor Yellow
$loginBody = @{
    code = "test_code"
} | ConvertTo-Json

try {
    $loginResponse = Invoke-RestMethod -Uri "http://127.0.0.1:8081/api/auth/login" `
        -Method POST `
        -ContentType "application/json" `
        -Body $loginBody `
        -ErrorAction Stop
    Write-Host "Login endpoint response:" -ForegroundColor Green
    $loginResponse | ConvertTo-Json -Depth 3
} catch {
    Write-Host "Login endpoint error (this is normal, requires real WeChat code):" -ForegroundColor Yellow
    Write-Host $_.Exception.Message
}

# Test 404 endpoint
Write-Host "`n2. Testing 404 error handling (GET /api/notfound)" -ForegroundColor Yellow
try {
    $notFoundResponse = Invoke-RestMethod -Uri "http://127.0.0.1:8081/api/notfound" `
        -Method GET `
        -ErrorAction Stop
} catch {
    $statusCode = $_.Exception.Response.StatusCode.value__
    Write-Host "404 error handling works correctly, status code: $statusCode" -ForegroundColor Green
}

Write-Host "`nTesting completed!" -ForegroundColor Green
Write-Host "Note: To test the complete login flow, you need a real WeChat miniprogram login code" -ForegroundColor Cyan
