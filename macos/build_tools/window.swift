import AppKit
import WebKit

final class MixCutWindow: NSObject, NSApplicationDelegate, WKUIDelegate, WKNavigationDelegate {
    private let address: URL
    private let smokeTest: Bool
    private var mainWindow: NSWindow!
    private var secondaryWindows: [NSWindow] = []

    init(address: URL, smokeTest: Bool) {
        self.address = address
        self.smokeTest = smokeTest
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
        installMenu(app)

        let frame = NSRect(x: 0, y: 0, width: 1440, height: 900)
        mainWindow = NSWindow(contentRect: frame,
                              styleMask: [.titled, .closable, .miniaturizable, .resizable],
                              backing: .buffered, defer: false)
        mainWindow.title = "MixCut Studio"
        mainWindow.minSize = NSSize(width: 900, height: 650)
        mainWindow.center()
        mainWindow.isReleasedWhenClosed = false

        let browser = WKWebView(frame: frame)
        browser.uiDelegate = self
        browser.navigationDelegate = self
        browser.allowsBackForwardNavigationGestures = true
        mainWindow.contentView = browser
        mainWindow.makeKeyAndOrderFront(nil)
        app.activate(ignoringOtherApps: true)
        browser.load(URLRequest(url: address))
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard smokeTest else { return }
        webView.callAsyncJavaScript("""
            const response = await fetch('/api/bootstrap');
            const data = await response.json();
            return document.querySelector('#scan-btn') !== null && data.api_protocol === 3;
            """, arguments: [:], in: nil, in: .page) { result in
            switch result {
            case .success(let valid) where valid as? Bool == true:
                print("native_window_smoke=ok", terminator: "\n")
                NSApplication.shared.terminate(nil)
            case .success, .failure:
                fputs("Native window could not load the current MixCut UI.\n", stderr)
                exit(1)
            }
        }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        if smokeTest {
            fputs("Native window failed to load: \(error)\n", stderr)
            exit(1)
        }
    }

    private func installMenu(_ app: NSApplication) {
        let menu = NSMenu()
        let appItem = NSMenuItem()
        menu.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "退出 MixCut Studio", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        let editItem = NSMenuItem()
        menu.addItem(editItem)
        let editMenu = NSMenu(title: "编辑")
        editMenu.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editMenu.addItem(withTitle: "复制", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editItem.submenu = editMenu
        app.mainMenu = menu
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.beginSheetModal(for: webView.window ?? mainWindow) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "确定")
        alert.addButton(withTitle: "取消")
        alert.beginSheetModal(for: webView.window ?? mainWindow) { result in
            completionHandler(result == .alertFirstButtonReturn)
        }
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String,
                 defaultText: String?, initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (String?) -> Void) {
        let alert = NSAlert()
        alert.messageText = prompt
        alert.addButton(withTitle: "确认")
        alert.addButton(withTitle: "取消")
        let input = NSTextField(string: defaultText ?? "")
        input.frame = NSRect(x: 0, y: 0, width: 340, height: 26)
        alert.accessoryView = input
        alert.beginSheetModal(for: webView.window ?? mainWindow) { result in
            completionHandler(result == .alertFirstButtonReturn ? input.stringValue : nil)
        }
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.beginSheetModal(for: webView.window ?? mainWindow) { result in
            completionHandler(result == .OK ? panel.urls : nil)
        }
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        let frame = NSRect(x: 0, y: 0, width: 900, height: 700)
        let window = NSWindow(contentRect: frame,
                              styleMask: [.titled, .closable, .miniaturizable, .resizable],
                              backing: .buffered, defer: false)
        window.title = "MixCut Studio"
        window.center()
        window.isReleasedWhenClosed = false
        let browser = WKWebView(frame: frame, configuration: configuration)
        browser.uiDelegate = self
        window.contentView = browser
        secondaryWindows.append(window)
        window.makeKeyAndOrderFront(nil)
        return browser
    }
}

guard (2...3).contains(CommandLine.arguments.count),
      let address = URL(string: CommandLine.arguments[1]),
      address.host == "127.0.0.1",
      address.scheme == "http",
      (CommandLine.arguments.count == 2 || CommandLine.arguments[2] == "--smoke") else {
    fputs("Expected a local MixCut Studio URL.\n", stderr)
    exit(2)
}

let app = NSApplication.shared
let delegate = MixCutWindow(address: address, smokeTest: CommandLine.arguments.count == 3)
app.delegate = delegate
app.run()
