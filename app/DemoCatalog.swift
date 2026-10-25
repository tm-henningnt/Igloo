import Foundation

enum DemoCatalog {
    private static func resource(_ name: String) -> Data {
        try! Data(contentsOf: Bundle.main.url(forResource: name, withExtension: "json")!)
    }
    static func snapshot() -> Snapshot {
        try! JSONDecoder().decode(Snapshot.self, from: resource("demo-catalog"))
    }
    static func reply<T: Decodable>(_ args: [String], as type: T.Type) -> T? {
        var data: Data
        switch args.first {
        case "snapshot": data = resource("demo-catalog")
        case "task-list": data = resource("demo-tasks")
        case "task-history":
            let object = try! JSONSerialization.jsonObject(with: resource("demo-tasks")) as! [String: Any]
            let items = object["tasks"] as! [[String: Any]]
            data = try! JSONSerialization.data(withJSONObject: ["runs": items.compactMap { $0["lastRun"] }])
        default: return nil
        }
        return try? JSONDecoder().decode(type, from: data)
    }
}
