using NLog;
using NLog.Config;
using NLog.Targets;

var toolId = Environment.GetEnvironmentVariable("TOOL_ID") ?? "unknown";
var logDir = "/var/log/app";

Directory.CreateDirectory(logDir);

// Configure NLog programmatically
var config = new LoggingConfiguration();
var fileTarget = new FileTarget("file")
{
    FileName = $"{logDir}/app.log",
    Layout = "${longdate} [${level:uppercase=true}] [toolid=" + toolId + "] ${message}${onexception:${newline}${exception:format=tostring}}"
};
config.AddRule(LogLevel.Debug, LogLevel.Fatal, fileTarget);
LogManager.Configuration = config;

var logger = LogManager.GetCurrentClassLogger();

logger.Info("Application started, toolId={ToolId}", toolId);

var random = new Random();
var levels = new[] { "Info", "Warn", "Error", "Debug" };
var messages = new[]
{
    "Cycle started",
    "Sensor reading OK",
    "Processing wafer batch",
    "Alignment check passed",
    "Pressure nominal",
    "Temperature out of range",
    "Retrying operation",
    "Cycle completed successfully",
};

while (true)
{
    var msg = messages[random.Next(messages.Length)];
    var level = levels[random.Next(levels.Length)];

    switch (level)
    {
        case "Info":  logger.Info(msg); break;
        case "Warn":  logger.Warn(msg); break;
        case "Error": logger.Error(msg); break;
        case "Debug": logger.Debug(msg); break;
    }

    await Task.Delay(TimeSpan.FromSeconds(random.Next(2, 6)));
}
