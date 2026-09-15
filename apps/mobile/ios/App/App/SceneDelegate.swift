import UIKit
import Capacitor

class SceneDelegate: UIResponder, UIWindowSceneDelegate {
    var window: UIWindow?

    func scene(_ scene: UIScene, willConnectTo session: UISceneSession, options connectionOptions: UIScene.ConnectionOptions) {
        guard let windowScene = scene as? UIWindowScene else { return }

        window = UIWindow(windowScene: windowScene)
        window?.rootViewController = CompassViewController()
        window?.makeKeyAndVisible()

        SceneDelegateProxy.shared.scene(scene, willConnectTo: session, options: connectionOptions)
    }

    func scene(_ scene: UIScene, openURLContexts URLContexts: Set<UIOpenURLContext>) {
        SceneDelegateProxy.shared.scene(scene, openURLContexts: URLContexts)
    }

    func scene(_ scene: UIScene, continue userActivity: NSUserActivity) {
        SceneDelegateProxy.shared.scene(scene, continue: userActivity)
    }
}

import Security
import CryptoKit

class CompassViewController: CAPBridgeViewController {
    override func capacitorDidLoad() { bridge?.registerPluginInstance(CompassPlugin()) }
}

private enum CompassError: LocalizedError {
    case message(String)
    var errorDescription: String? { if case let .message(text) = self { return text }; return nil }
}

private final class PinnedDelegate: NSObject, URLSessionDelegate, URLSessionTaskDelegate {
    let fingerprint: String
    init(_ fingerprint: String) { self.fingerprint = fingerprint }
    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust,
              let cert = SecTrustGetCertificateAtIndex(trust, 0) else {
            completionHandler(.cancelAuthenticationChallenge, nil); return
        }
        let digest = SHA256.hash(data: SecCertificateCopyData(cert) as Data).map { String(format: "%02x", $0) }.joined()
        guard digest == fingerprint else { completionHandler(.cancelAuthenticationChallenge, nil); return }
        // The paired certificate is the identity. Also enforce its validity period.
        SecTrustSetAnchorCertificates(trust, [cert] as CFArray)
        SecTrustSetAnchorCertificatesOnly(trust, true)
        SecTrustSetPolicies(trust, SecPolicyCreateBasicX509())
        guard SecTrustEvaluateWithError(trust, nil) else { completionHandler(.cancelAuthenticationChallenge, nil); return }
        completionHandler(.useCredential, URLCredential(trust: trust))
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

@objc(CompassPlugin)
public class CompassPlugin: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "CompassPlugin"
    public let jsName = "Compass"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "connection", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "pair", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "request", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "forget", returnType: CAPPluginReturnPromise)
    ]
    private let work = DispatchQueue(label: "compass.credentials")
    private let service = "com.tradecompass.mobile.dev.device.v1"
    private var query: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
         kSecAttrAccount as String: "computer"]
    }
    private func state() throws -> [String: Any]? {
        var attributes = query
        attributes[kSecReturnData as String] = true
        attributes[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(attributes as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data,
              let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw CompassError.message("无法读取设备凭据，请重新配对")
        }
        return object
    }
    private func save(_ object: [String: Any]) throws {
        let data = try JSONSerialization.data(withJSONObject: object)
        var item = query
        item[kSecValueData as String] = data
        item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let status = SecItemAdd(item as CFDictionary, nil)
        guard status == errSecSuccess else { throw CompassError.message("无法保存设备凭据") }
    }
    private func validate(_ object: [String: Any]) throws -> URL {
        guard let endpoint = object["endpoint"] as? String, let url = URL(string: endpoint),
              url.scheme == "https", url.host != nil, url.user == nil, url.password == nil,
              url.query == nil, url.fragment == nil, (url.path.isEmpty || url.path == "/"),
              let pin = object["certificate_sha256"] as? String,
              pin.range(of: "^[a-f0-9]{64}$", options: .regularExpression) != nil else {
            throw CompassError.message("电脑连接信息无效")
        }
        return url
    }
    private func send(_ state: [String: Any], method: String, path: String, body: String?,
                      authorize: Bool, call: CAPPluginCall) throws {
        let origin = try validate(state)
        guard ["GET", "POST"].contains(method), path.hasPrefix("/mobile/v1/"), !path.contains(".."),
              let url = URL(string: path, relativeTo: origin)?.absoluteURL,
              url.host == origin.host, url.port == origin.port, url.scheme == origin.scheme else {
            throw CompassError.message("请求无效")
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = 20
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if authorize, let secret = state["device_secret"] as? String {
            request.setValue("Bearer \(secret)", forHTTPHeaderField: "Authorization")
        }
        request.httpBody = body?.data(using: .utf8)
        let config = URLSessionConfiguration.ephemeral
        config.urlCache = nil
        config.httpShouldSetCookies = false
        let session = URLSession(configuration: config,
            delegate: PinnedDelegate(state["certificate_sha256"] as! String), delegateQueue: nil)
        session.dataTask(with: request) { data, response, error in
            defer { session.finishTasksAndInvalidate() }
            guard error == nil, let response = response as? HTTPURLResponse, let data = data,
                  data.count <= 16 * 1024 * 1024,
                  let object = try? JSONSerialization.jsonObject(with: data) else {
                call.reject("连接未完成，请检查电脑是否开机、网络是否可达，以及配对身份是否一致"); return
            }
            call.resolve(["status": response.statusCode, "data": object])
        }.resume()
    }
    @objc func connection(_ call: CAPPluginCall) { work.async {
        do { if let state = try self.state() {
            call.resolve(["connected": true, "endpoint": state["endpoint"] ?? "", "computer_id": state["computer_id"] ?? ""])
        } else { call.resolve(["connected": false]) }
        } catch { call.reject(error.localizedDescription) }
    } }
    @objc func pair(_ call: CAPPluginCall) { work.async {
        do {
            guard try self.state() == nil else { throw CompassError.message("请先移除已有电脑连接") }
            guard let text = call.getString("invitation"), let data = text.data(using: .utf8),
                  let invite = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  invite["protocol_version"] as? Int == 1,
                  let expires = invite["expires_at"] as? Double, expires > Date().timeIntervalSince1970,
                  let invitation = invite["invitation"] as? String,
                  invitation.range(of: "^[A-Za-z0-9_-]{43}$", options: .regularExpression) != nil,
                  let computer = invite["computer_id"] as? String else { throw CompassError.message("配对信息已过期或无效") }
            _ = try self.validate(invite)
            var random = [UInt8](repeating: 0, count: 32)
            guard SecRandomCopyBytes(kSecRandomDefault, random.count, &random) == errSecSuccess else {
                throw CompassError.message("无法生成设备凭据")
            }
            let secret = Data(random).base64EncodedString().replacingOccurrences(of: "+", with: "-")
                .replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
            let state: [String: Any] = ["endpoint": invite["endpoint"]!, "certificate_sha256": invite["certificate_sha256"]!,
                                       "computer_id": computer, "device_secret": secret]
            try self.save(state)
            let body = try JSONSerialization.data(withJSONObject: ["invitation": invitation, "device_secret": secret,
                                                                   "name": call.getString("name") ?? "iPhone"])
            try self.send(state, method: "POST", path: "/mobile/v1/pairing/claim",
                          body: String(data: body, encoding: .utf8), authorize: false, call: call)
        } catch { call.reject(error.localizedDescription) }
    } }
    @objc func request(_ call: CAPPluginCall) { work.async {
        do {
            guard let state = try self.state() else { throw CompassError.message("请先连接电脑") }
            try self.send(state, method: call.getString("method") ?? "GET", path: call.getString("path") ?? "",
                          body: call.getString("body"), authorize: true, call: call)
        } catch { call.reject(error.localizedDescription) }
    } }
    @objc func forget(_ call: CAPPluginCall) { work.async {
        let status = SecItemDelete(self.query as CFDictionary)
        if status == errSecSuccess || status == errSecItemNotFound { call.resolve() }
        else { call.reject("无法移除连接，请重试") }
    } }
}
