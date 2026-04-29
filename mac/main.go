package main

import (
	"bufio"
	"encoding/base64"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"

	"golang.getoutline.org/sdk/x/mobileproxy"
)

type OutlineKey struct {
	Method     string `json:"method"`
	Password   string `json:"password"`
	Server     string `json:"server"`
	ServerPort int    `json:"server_port"`
	Prefix     string `json:"prefix,omitempty"`
	Tag        string `json:"tag,omitempty"`
}

func main() {
	configPath := flag.String("config", "config.json", "path to JSON key file (used if keys/ is empty)")
	ssconfURL := flag.String("ssconf", "", "ssconf:// URL (downloads JSON, overrides -config and keys/)")
	keyName := flag.String("key", "", "name of key in keys/ folder (filename without .json)")
	keysDir := flag.String("keys-dir", "keys", "folder with multiple JSON keys")
	addr := flag.String("addr", "127.0.0.1:8080", "local HTTP proxy address")
	launch := flag.String("launch", "", "what to start: code | bash | terminal | <path>. Empty = interactive menu")
	noMenu := flag.Bool("no-menu", false, "skip menu, just run proxy")
	flag.Parse()

	key, source, err := selectKey(*configPath, *ssconfURL, *keyName, *keysDir)
	if err != nil {
		fmt.Println()
		fmt.Println("Не нашёл ни одного VPN-ключа.")
		fmt.Println()
		fmt.Println("Что можно сделать:")
		fmt.Println("  1) Положи .json от Outline в папку  ", *keysDir, "/")
		fmt.Println("  2) Или передай ссылку:  ./vpn-proxy -ssconf \"ssconf://...\"")
		fmt.Println("  3) Или используй GUI (gui.bat / VPN Proxy.app) — там кнопка ‘+ ssconf://’")
		fmt.Println()
		fmt.Println("Образец ключа:", *keysDir+"/example.json.template")
		fmt.Println()
		log.Fatalf("error: %v", err)
	}
	log.Printf("key source: %s", source)

	transport, err := buildTransport(key)
	if err != nil {
		log.Fatalf("build transport: %v", err)
	}

	tag := key.Tag
	if tag == "" {
		tag = key.Server
	}
	log.Printf("server: %s  (%s:%d, prefix=%t)", tag, key.Server, key.ServerPort, key.Prefix != "")

	dialer, err := mobileproxy.NewStreamDialerFromConfig(transport)
	if err != nil {
		log.Fatalf("dialer: %v", err)
	}
	proxy, err := mobileproxy.RunProxy(*addr, dialer)
	if err != nil {
		log.Fatalf("run proxy: %v", err)
	}
	proxyURL := "http://" + proxy.Address()
	log.Printf("HTTP proxy listening on %s", proxyURL)

	choice := *launch
	if choice == "" && !*noMenu {
		choice = askMenu()
	}
	if choice != "" && choice != "skip" {
		if err := launchProgram(choice, proxyURL); err != nil {
			log.Printf("launch failed: %v", err)
		}
	}

	printHints(proxyURL)
	log.Print("Ctrl+C to stop")

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, os.Interrupt, syscall.SIGTERM)
	<-sig
	log.Print("shutting down")
	proxy.Stop(2)
}

// selectKey resolves where to take the VPN key from:
//  1. -ssconf URL takes priority (downloads from web)
//  2. then -key <name> matches a file in keys/ folder
//  3. then keys/ folder: 1 file → use it, multiple → menu
//  4. fall back to -config <path> (default ./config.json)
func selectKey(configPath, ssconfURL, keyName, keysDir string) (*OutlineKey, string, error) {
	if ssconfURL != "" {
		log.Printf("fetching config from %s", ssconfURL)
		data, err := fetchSSConf(ssconfURL)
		if err != nil {
			return nil, ssconfURL, err
		}
		k, err := parseKey(data)
		return k, ssconfURL, err
	}

	jsons := listKeyFiles(keysDir)
	if len(jsons) > 0 {
		var picked string
		switch {
		case keyName != "":
			for _, j := range jsons {
				if strings.EqualFold(strings.TrimSuffix(j, ".json"), keyName) {
					picked = j
					break
				}
			}
			if picked == "" {
				return nil, "", fmt.Errorf("no key %q in %s/ (available: %s)", keyName, keysDir, strings.Join(stripExt(jsons), ", "))
			}
		case len(jsons) == 1:
			picked = jsons[0]
		default:
			picked = askKeyMenu(keysDir, jsons)
		}
		path := filepath.Join(keysDir, picked)
		k, err := loadKeyFile(path)
		return k, path, err
	}

	k, err := loadKeyFile(configPath)
	return k, configPath, err
}

func listKeyFiles(dir string) []string {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	var out []string
	for _, e := range entries {
		if !e.IsDir() && strings.HasSuffix(strings.ToLower(e.Name()), ".json") {
			out = append(out, e.Name())
		}
	}
	return out
}

func stripExt(names []string) []string {
	out := make([]string, len(names))
	for i, n := range names {
		out[i] = strings.TrimSuffix(n, ".json")
	}
	return out
}

func askKeyMenu(keysDir string, jsons []string) string {
	fmt.Println()
	fmt.Println("Доступные ключи в", keysDir+"/:")
	for i, j := range jsons {
		// Try to read tag for nicer label
		label := strings.TrimSuffix(j, ".json")
		if k, err := loadKeyFile(filepath.Join(keysDir, j)); err == nil && k.Tag != "" {
			label = fmt.Sprintf("%s  (%s)", label, k.Tag)
		}
		fmt.Printf("  %d) %s\n", i+1, label)
	}
	fmt.Print("> ")
	r := bufio.NewReader(os.Stdin)
	line, _ := r.ReadString('\n')
	line = strings.TrimSpace(line)
	if n, err := strconv.Atoi(line); err == nil && n >= 1 && n <= len(jsons) {
		return jsons[n-1]
	}
	return jsons[0]
}

func loadKeyFile(path string) (*OutlineKey, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return parseKey(data)
}

func parseKey(data []byte) (*OutlineKey, error) {
	var k OutlineKey
	if err := json.Unmarshal(sanitizeJSON(data), &k); err != nil {
		return nil, fmt.Errorf("parse JSON: %w", err)
	}
	if k.Method == "" || k.Password == "" || k.Server == "" || k.ServerPort == 0 {
		return nil, fmt.Errorf("incomplete config (need method, password, server, server_port)")
	}
	return &k, nil
}

func fetchSSConf(ssconfURL string) ([]byte, error) {
	httpsURL := strings.Replace(ssconfURL, "ssconf://", "https://", 1)
	resp, err := http.Get(httpsURL)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("HTTP %d from %s", resp.StatusCode, httpsURL)
	}
	return io.ReadAll(resp.Body)
}

// Outline JSON often contains raw control bytes inside the "prefix" string,
// which strict json.Unmarshal rejects. Escape any 0x00-0x1F bytes that appear
// inside JSON strings before parsing.
func sanitizeJSON(data []byte) []byte {
	out := make([]byte, 0, len(data)+16)
	inString := false
	escaped := false
	for _, b := range data {
		if !inString {
			if b == '"' {
				inString = true
			}
			out = append(out, b)
			continue
		}
		if escaped {
			escaped = false
			out = append(out, b)
			continue
		}
		if b == '\\' {
			escaped = true
			out = append(out, b)
			continue
		}
		if b == '"' {
			inString = false
			out = append(out, b)
			continue
		}
		if b < 0x20 {
			out = append(out, []byte(fmt.Sprintf("\\u%04x", b))...)
			continue
		}
		out = append(out, b)
	}
	return out
}

func buildTransport(k *OutlineKey) (string, error) {
	// SIP002 Shadowsocks URI: ss://base64url(method:password)@host:port
	// Prefix is the UTF-8 string from JSON: each rune is a byte 0-255 (Latin-1 as Unicode).
	// URL-encode the UTF-8 bytes; SDK will URL-decode and []rune-iterate to recover bytes.
	userinfo := base64.RawURLEncoding.EncodeToString([]byte(k.Method + ":" + k.Password))
	u := fmt.Sprintf("ss://%s@%s:%d", userinfo, k.Server, k.ServerPort)
	if k.Prefix != "" {
		u += "/?prefix=" + url.QueryEscape(k.Prefix)
	}
	return u, nil
}

func askMenu() string {
	fmt.Println()
	fmt.Println("Что запустить через прокси?")
	fmt.Println("  1) VSCode")
	if runtime.GOOS == "windows" {
		fmt.Println("  2) Git Bash")
	} else {
		fmt.Println("  2) Terminal")
	}
	fmt.Println("  3) указать путь вручную")
	fmt.Println("  4) ничего, просто держать прокси")
	fmt.Print("> ")

	r := bufio.NewReader(os.Stdin)
	line, _ := r.ReadString('\n')
	line = strings.TrimSpace(line)

	switch line {
	case "1":
		return "code"
	case "2":
		if runtime.GOOS == "windows" {
			return "bash"
		}
		return "terminal"
	case "3":
		fmt.Print("путь к программе: ")
		p, _ := r.ReadString('\n')
		return strings.TrimSpace(p)
	default:
		return "skip"
	}
}

func launchProgram(name, proxyURL string) error {
	var cmd *exec.Cmd
	switch strings.ToLower(name) {
	case "code", "vscode":
		cmd = vscodeCmd()
	case "bash", "git-bash", "gitbash":
		cmd = gitBashCmd()
	case "terminal":
		cmd = terminalCmd()
	default:
		cmd = exec.Command(name)
	}
	if cmd == nil {
		return fmt.Errorf("don't know how to launch %q on %s", name, runtime.GOOS)
	}
	cmd.Env = append(os.Environ(),
		"HTTP_PROXY="+proxyURL,
		"HTTPS_PROXY="+proxyURL,
		"http_proxy="+proxyURL,
		"https_proxy="+proxyURL,
		"ALL_PROXY="+proxyURL,
		"NO_PROXY=localhost,127.0.0.1",
	)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	log.Printf("launching: %s", strings.Join(cmd.Args, " "))
	return cmd.Start()
}

func vscodeCmd() *exec.Cmd {
	if runtime.GOOS == "windows" {
		// 'code' is code.cmd on Windows, needs cmd /c
		return exec.Command("cmd", "/c", "start", "", "code")
	}
	if runtime.GOOS == "darwin" {
		// On Mac, 'code' is in PATH if shell command was installed
		if path, err := exec.LookPath("code"); err == nil {
			return exec.Command(path)
		}
		return exec.Command("open", "-a", "Visual Studio Code")
	}
	return exec.Command("code")
}

func gitBashCmd() *exec.Cmd {
	if runtime.GOOS != "windows" {
		return exec.Command("bash")
	}
	candidates := []string{
		`C:\Program Files\Git\git-bash.exe`,
		`C:\Program Files (x86)\Git\git-bash.exe`,
		filepath.Join(os.Getenv("LOCALAPPDATA"), `Programs\Git\git-bash.exe`),
	}
	for _, p := range candidates {
		if _, err := os.Stat(p); err == nil {
			return exec.Command(p)
		}
	}
	if path, err := exec.LookPath("git-bash.exe"); err == nil {
		return exec.Command(path)
	}
	return nil
}

func terminalCmd() *exec.Cmd {
	switch runtime.GOOS {
	case "darwin":
		return exec.Command("open", "-a", "Terminal")
	case "linux":
		for _, t := range []string{"x-terminal-emulator", "gnome-terminal", "konsole", "xterm"} {
			if path, err := exec.LookPath(t); err == nil {
				return exec.Command(path)
			}
		}
	case "windows":
		return exec.Command("cmd", "/c", "start", "", "wt.exe")
	}
	return nil
}

func printHints(proxyURL string) {
	fmt.Println()
	fmt.Println("---")
	fmt.Println("Если запускаешь программу руками, выстави прокси:")
	if runtime.GOOS == "windows" {
		fmt.Printf("  PowerShell : $env:HTTPS_PROXY=%q ; $env:HTTP_PROXY=%q\n", proxyURL, proxyURL)
		fmt.Printf("  Git Bash   : export HTTPS_PROXY=%s ; export HTTP_PROXY=%s\n", proxyURL, proxyURL)
	} else {
		fmt.Printf("  bash/zsh   : export HTTPS_PROXY=%s ; export HTTP_PROXY=%s\n", proxyURL, proxyURL)
	}
	fmt.Printf("  git только : git config --global http.proxy %s\n", proxyURL)
	fmt.Printf("  VSCode     : settings.json -> \"http.proxy\": %q\n", proxyURL)
	fmt.Println("---")
}
