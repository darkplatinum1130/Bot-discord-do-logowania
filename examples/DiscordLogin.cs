// Przykład klienta w C# (.NET 6+) – np. do launchera WPF/WinForms/konsoli.
// Użycie:
//   var login = new DiscordLogin("http://localhost:8080", "app_WPISZ_TUTAJ");
//   var start = await login.StartAsync();
//   Console.WriteLine($"Wpisz na Discordzie: /login {start.Code}");
//   var result = await login.WaitForApprovalAsync(start);   // albo: await login.RedeemAsync(kodZ_kod)
//   if (result.Token != null) { var me = await login.MeAsync(result.Token); Console.WriteLine(me?.User?.DisplayName); }
using System;
using System.Collections.Generic;
using System.Net.Http;
using System.Net.Http.Json;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading;
using System.Threading.Tasks;

public sealed class DiscordLogin
{
    private readonly HttpClient _http;
    private readonly string _appId;
    private static readonly JsonSerializerOptions Json = new() { PropertyNameCaseInsensitive = true };

    public DiscordLogin(string apiUrl, string appId)
    {
        _appId = appId;
        _http = new HttpClient { BaseAddress = new Uri(apiUrl.TrimEnd('/') + "/"), Timeout = TimeSpan.FromSeconds(15) };
        _http.DefaultRequestHeaders.Add("X-HWID", GetHwid());
    }

    /// <summary>Stały identyfikator komputera (hash MachineGuid z rejestru).</summary>
    public string GetHwid()
    {
        string raw = Environment.MachineName;
        if (OperatingSystem.IsWindows())
        {
            using var key = Microsoft.Win32.RegistryKey
                .OpenBaseKey(Microsoft.Win32.RegistryHive.LocalMachine, Microsoft.Win32.RegistryView.Registry64)
                .OpenSubKey(@"SOFTWARE\Microsoft\Cryptography");
            raw = key?.GetValue("MachineGuid")?.ToString() ?? raw;
        }
        var hash = SHA256.HashData(Encoding.UTF8.GetBytes($"{_appId}:{raw}"));
        return Convert.ToHexString(hash).ToLowerInvariant();
    }

    // Tryb 1: aplikacja pokazuje kod, użytkownik wpisuje /login KOD na Discordzie
    public async Task<StartResponse> StartAsync()
    {
        var res = await _http.PostAsJsonAsync("api/auth/start", new { app_id = _appId, hwid = GetHwid() });
        return await Read<StartResponse>(res);
    }

    public async Task<LoginResult> WaitForApprovalAsync(StartResponse start, CancellationToken ct = default)
    {
        while (!ct.IsCancellationRequested)
        {
            await Task.Delay(TimeSpan.FromSeconds(start.Interval), ct);
            var result = await Read<LoginResult>(await _http.GetAsync($"api/auth/poll/{start.RequestId}", ct));
            if (result.Status != "pending") return result;
        }
        return new LoginResult { Status = "cancelled" };
    }

    // Tryb 2: użytkownik wpisuje /kod na Discordzie i przepisuje kod do aplikacji
    public async Task<LoginResult> RedeemAsync(string code)
    {
        var res = await _http.PostAsJsonAsync("api/auth/redeem", new { app_id = _appId, code, hwid = GetHwid() });
        var result = await Read<LoginResult>(res);
        result.Status ??= res.IsSuccessStatusCode ? "approved" : "denied";
        return result;
    }

    /// <summary>Sprawdza zapisany token. Zwraca null, jeśli trzeba zalogować się ponownie.</summary>
    public async Task<MeResponse?> MeAsync(string token)
    {
        using var req = new HttpRequestMessage(HttpMethod.Get, "api/auth/me");
        req.Headers.Authorization = new("Bearer", token);
        var res = await _http.SendAsync(req);
        return res.IsSuccessStatusCode ? await Read<MeResponse>(res) : null;
    }

    public async Task LogoutAsync(string token)
    {
        using var req = new HttpRequestMessage(HttpMethod.Post, "api/auth/logout");
        req.Headers.Authorization = new("Bearer", token);
        await _http.SendAsync(req);
    }

    private static async Task<T> Read<T>(HttpResponseMessage res) where T : new()
        => await res.Content.ReadFromJsonAsync<T>(Json) ?? new T();
}

public sealed class StartResponse
{
    [JsonPropertyName("request_id")] public string RequestId { get; set; } = "";
    public string Code { get; set; } = "";
    [JsonPropertyName("expires_in")] public int ExpiresIn { get; set; }
    public int Interval { get; set; } = 2;
    public string? Error { get; set; }
    public string? Message { get; set; }
}

public sealed class LoginResult
{
    public string? Status { get; set; }     // approved | denied | expired | pending
    public string? Token { get; set; }
    [JsonPropertyName("expires_at")] public long ExpiresAt { get; set; }
    public DiscordUser? User { get; set; }
    public string? Reason { get; set; }     // np. missing_role, banned, hwid_mismatch
    public string? Error { get; set; }
    public string? Message { get; set; }    // komunikat po polsku do pokazania użytkownikowi
}

public sealed class MeResponse
{
    public bool Valid { get; set; }
    [JsonPropertyName("expires_at")] public long ExpiresAt { get; set; }
    public DiscordUser? User { get; set; }
}

public sealed class DiscordUser
{
    public string Id { get; set; } = "";
    public string Username { get; set; } = "";
    [JsonPropertyName("display_name")] public string DisplayName { get; set; } = "";
    [JsonPropertyName("avatar_url")] public string AvatarUrl { get; set; } = "";
    public List<DiscordRole> Roles { get; set; } = new();
}

public sealed class DiscordRole
{
    public string Id { get; set; } = "";
    public string Name { get; set; } = "";
}
