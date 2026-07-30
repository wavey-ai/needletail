use anyhow::{Context, Result};
use clap::Parser;
use needletail::operations_entrypoint::{OperationsEntrypointPolicy, OPERATIONS_WELL_KNOWN_PATH};
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::time::timeout;

const MAX_REQUEST_HEAD_BYTES: usize = 16 * 1024;
const REQUEST_TIMEOUT: Duration = Duration::from_secs(3);
const DEFAULT_MAX_CONNECTIONS: usize = 128;

#[derive(Debug, Parser)]
#[command(
    name = "needletail-ops-entrypoint",
    about = "Redirect Needletail Operations discovery to the quorum-elected collector"
)]
struct Args {
    #[arg(long, env = "NEEDLETAIL_OPS_LISTEN", default_value = "127.0.0.1:19449")]
    listen: SocketAddr,

    #[arg(long, env = "NEEDLETAIL_OPS_ASSIGNMENT_FILE")]
    assignment_file: PathBuf,

    #[arg(long, env = "NEEDLETAIL_OPS_AUTHORITY")]
    authority: String,

    #[arg(long, env = "NEEDLETAIL_OPS_ENTRYPOINT_HOST")]
    entrypoint_host: String,

    #[arg(
        long = "allowed-endpoint",
        env = "NEEDLETAIL_OPS_ALLOWED_ENDPOINTS",
        value_delimiter = ',',
        required = true
    )]
    allowed_endpoints: Vec<String>,

    #[arg(
        long,
        env = "NEEDLETAIL_OPS_LEASE_SAFETY_MARGIN_MS",
        default_value_t = 5_000
    )]
    lease_safety_margin_ms: u64,

    #[arg(
        long,
        env = "NEEDLETAIL_OPS_MAX_CLOCK_SKEW_MS",
        default_value_t = 1_000
    )]
    max_clock_skew_ms: u64,

    #[arg(
        long,
        env = "NEEDLETAIL_OPS_MAX_LEASE_DURATION_MS",
        default_value_t = 30_000
    )]
    max_lease_duration_ms: u64,

    #[arg(
        long,
        env = "NEEDLETAIL_OPS_MAX_CONNECTIONS",
        default_value_t = DEFAULT_MAX_CONNECTIONS
    )]
    max_connections: usize,
}

#[derive(Clone)]
struct AppState {
    assignment_file: PathBuf,
    policy: OperationsEntrypointPolicy,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum RequestMethod {
    Get,
    Head,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum RequestRoute {
    Discover,
    Ready,
    Live,
    Missing,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum RequestError {
    BadRequest,
    MethodNotAllowed,
    VersionNotSupported,
}

struct ConnectionGuard(Arc<AtomicUsize>);

impl Drop for ConnectionGuard {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::Relaxed);
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    if args.max_connections == 0 {
        anyhow::bail!("--max-connections must be greater than zero");
    }
    if !args.listen.ip().is_loopback() {
        anyhow::bail!(
            "--listen must use loopback; terminate public TLS in the global reverse proxy"
        );
    }
    let policy = OperationsEntrypointPolicy::new(
        args.authority,
        args.entrypoint_host,
        args.allowed_endpoints,
        args.lease_safety_margin_ms,
        args.max_clock_skew_ms,
        args.max_lease_duration_ms,
    )
    .context("invalid Operations entry point policy")?;
    let state = Arc::new(AppState {
        assignment_file: args.assignment_file,
        policy,
    });
    let listener = TcpListener::bind(args.listen)
        .await
        .with_context(|| format!("failed to bind Operations entry point to {}", args.listen))?;
    let active_connections = Arc::new(AtomicUsize::new(0));

    println!(
        "Needletail Operations entry point listening on {}",
        args.listen
    );
    loop {
        let (stream, _) = listener
            .accept()
            .await
            .context("failed to accept Operations entry point connection")?;
        let active = active_connections.fetch_add(1, Ordering::Relaxed) + 1;
        if active > args.max_connections {
            active_connections.fetch_sub(1, Ordering::Relaxed);
            continue;
        }
        let guard = ConnectionGuard(Arc::clone(&active_connections));
        let state = Arc::clone(&state);
        tokio::spawn(async move {
            let _guard = guard;
            let _ = serve_connection(stream, &state).await;
        });
    }
}

async fn serve_connection(mut stream: TcpStream, state: &AppState) -> Result<()> {
    stream.set_nodelay(true)?;
    let request = match timeout(REQUEST_TIMEOUT, read_request_head(&mut stream)).await {
        Ok(Ok(request)) => request,
        Ok(Err(error)) => {
            write_response(&mut stream, error_response(error), RequestMethod::Get).await?;
            return Ok(());
        }
        Err(_) => return Ok(()),
    };
    let (method, route) = match parse_request(&request) {
        Ok(request) => request,
        Err(error) => {
            write_response(&mut stream, error_response(error), RequestMethod::Get).await?;
            return Ok(());
        }
    };
    let response = route_request(route, state);
    write_response(&mut stream, response, method).await?;
    Ok(())
}

async fn read_request_head(stream: &mut TcpStream) -> Result<Vec<u8>, RequestError> {
    let mut request = Vec::with_capacity(1024);
    let mut chunk = [0_u8; 1024];
    loop {
        let read = stream
            .read(&mut chunk)
            .await
            .map_err(|_| RequestError::BadRequest)?;
        if read == 0 {
            return Err(RequestError::BadRequest);
        }
        if request.len().saturating_add(read) > MAX_REQUEST_HEAD_BYTES {
            return Err(RequestError::BadRequest);
        }
        request.extend_from_slice(&chunk[..read]);
        if request.windows(4).any(|window| window == b"\r\n\r\n") {
            return Ok(request);
        }
    }
}

fn parse_request(request: &[u8]) -> Result<(RequestMethod, RequestRoute), RequestError> {
    let request = std::str::from_utf8(request).map_err(|_| RequestError::BadRequest)?;
    let request_line = request
        .split_once("\r\n")
        .map(|(line, _)| line)
        .ok_or(RequestError::BadRequest)?;
    let mut fields = request_line.split(' ');
    let method = fields.next().ok_or(RequestError::BadRequest)?;
    let target = fields.next().ok_or(RequestError::BadRequest)?;
    let version = fields.next().ok_or(RequestError::BadRequest)?;
    if fields.next().is_some() || method.is_empty() || target.is_empty() {
        return Err(RequestError::BadRequest);
    }
    if version != "HTTP/1.1" {
        return Err(RequestError::VersionNotSupported);
    }
    let method = match method {
        "GET" => RequestMethod::Get,
        "HEAD" => RequestMethod::Head,
        _ => return Err(RequestError::MethodNotAllowed),
    };
    let route = match target {
        "/" | OPERATIONS_WELL_KNOWN_PATH => RequestRoute::Discover,
        "/readyz" => RequestRoute::Ready,
        "/healthz" => RequestRoute::Live,
        _ => RequestRoute::Missing,
    };
    Ok((method, route))
}

fn route_request(route: RequestRoute, state: &AppState) -> HttpResponse {
    match route {
        RequestRoute::Live => HttpResponse::text(200, "OK\n"),
        RequestRoute::Ready => match resolve_collector(state) {
            Ok(_) => HttpResponse::text(200, "ready\n"),
            Err(()) => HttpResponse::unavailable(),
        },
        RequestRoute::Discover => match resolve_collector(state) {
            Ok(location) => HttpResponse::redirect(location),
            Err(()) => HttpResponse::unavailable(),
        },
        RequestRoute::Missing => HttpResponse::text(404, "Not found.\n"),
    }
}

fn resolve_collector(state: &AppState) -> Result<String, ()> {
    state
        .policy
        .resolve_file(&state.assignment_file, unix_time_ms())
        .map_err(|_| ())
}

fn unix_time_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .try_into()
        .unwrap_or(u64::MAX)
}

struct HttpResponse {
    status: u16,
    body: &'static str,
    location: Option<String>,
    allow: Option<&'static str>,
}

impl HttpResponse {
    fn text(status: u16, body: &'static str) -> Self {
        Self {
            status,
            body,
            location: None,
            allow: None,
        }
    }

    fn redirect(location: String) -> Self {
        Self {
            status: 307,
            body: "Redirecting to Needletail Operations.\n",
            location: Some(location),
            allow: None,
        }
    }

    fn unavailable() -> Self {
        Self {
            status: 503,
            body: "Needletail Operations is temporarily unavailable.\n",
            location: None,
            allow: None,
        }
    }
}

fn error_response(error: RequestError) -> HttpResponse {
    match error {
        RequestError::BadRequest => HttpResponse::text(400, "Bad request.\n"),
        RequestError::MethodNotAllowed => HttpResponse {
            status: 405,
            body: "Method not allowed.\n",
            location: None,
            allow: Some("GET, HEAD"),
        },
        RequestError::VersionNotSupported => {
            HttpResponse::text(505, "HTTP version not supported.\n")
        }
    }
}

async fn write_response(
    stream: &mut TcpStream,
    response: HttpResponse,
    method: RequestMethod,
) -> Result<()> {
    let reason = match response.status {
        200 => "OK",
        307 => "Temporary Redirect",
        400 => "Bad Request",
        404 => "Not Found",
        405 => "Method Not Allowed",
        503 => "Service Unavailable",
        505 => "HTTP Version Not Supported",
        _ => "Internal Server Error",
    };
    let mut head = format!(
        "HTTP/1.1 {} {}\r\n\
         Content-Type: text/plain; charset=utf-8\r\n\
         Content-Length: {}\r\n\
         Cache-Control: no-store, max-age=0\r\n\
         Referrer-Policy: no-referrer\r\n\
         X-Content-Type-Options: nosniff\r\n\
         Connection: close\r\n",
        response.status,
        reason,
        response.body.len()
    );
    if let Some(location) = response.location {
        head.push_str("Location: ");
        head.push_str(&location);
        head.push_str("\r\n");
    }
    if let Some(allow) = response.allow {
        head.push_str("Allow: ");
        head.push_str(allow);
        head.push_str("\r\n");
    }
    if response.status == 503 {
        head.push_str("Retry-After: 2\r\n");
    }
    head.push_str("\r\n");
    stream.write_all(head.as_bytes()).await?;
    if method == RequestMethod::Get {
        stream.write_all(response.body.as_bytes()).await?;
    }
    stream.shutdown().await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use needletail::operations_assignment_publisher::OperationsAssignmentPublisher;
    use needletail::operations_entrypoint::OperationsCollectorAssignment;
    use std::env;
    use std::fs;
    #[cfg(unix)]
    use std::os::unix::fs::PermissionsExt;
    use std::process;
    use std::sync::atomic::{AtomicU64, Ordering};

    static TEST_DIRECTORY_SEQUENCE: AtomicU64 = AtomicU64::new(0);

    #[test]
    fn only_get_and_head_can_use_discovery() {
        assert_eq!(
            parse_request(b"GET /.well-known/needletail-operations HTTP/1.1\r\n\r\n"),
            Ok((RequestMethod::Get, RequestRoute::Discover))
        );
        assert_eq!(
            parse_request(b"HEAD / HTTP/1.1\r\n\r\n"),
            Ok((RequestMethod::Head, RequestRoute::Discover))
        );
        assert_eq!(
            parse_request(b"POST / HTTP/1.1\r\n\r\n"),
            Err(RequestError::MethodNotAllowed)
        );
    }

    #[test]
    fn query_strings_and_lookalike_paths_do_not_redirect() {
        for request in [
            "GET /?next=https://attacker.example HTTP/1.1\r\n\r\n",
            "GET /.well-known/needletail-operations/ HTTP/1.1\r\n\r\n",
            "GET //attacker.example HTTP/1.1\r\n\r\n",
            "GET https://attacker.example/ HTTP/1.1\r\n\r\n",
        ] {
            assert_eq!(
                parse_request(request.as_bytes()),
                Ok((RequestMethod::Get, RequestRoute::Missing))
            );
        }
    }

    #[test]
    fn refuses_ambiguous_request_lines_and_old_http() {
        assert_eq!(
            parse_request(b"GET  / HTTP/1.1\r\n\r\n"),
            Err(RequestError::BadRequest)
        );
        assert_eq!(
            parse_request(b"GET / HTTP/1.0\r\n\r\n"),
            Err(RequestError::VersionNotSupported)
        );
    }

    #[test]
    fn discovery_redirects_only_while_the_published_fence_is_live() {
        let now = unix_time_ms();
        let sequence = TEST_DIRECTORY_SEQUENCE.fetch_add(1, Ordering::Relaxed);
        let directory =
            env::temp_dir().join(format!("needletail-ops-http-{}-{sequence}", process::id()));
        fs::create_dir(&directory).unwrap();
        #[cfg(unix)]
        fs::set_permissions(&directory, fs::Permissions::from_mode(0o700)).unwrap();
        let assignment_file = directory.join("operations-collector.json");
        let policy = OperationsEntrypointPolicy::new(
            "needletail-controller".to_owned(),
            "ops.example.test".to_owned(),
            vec!["https://ops-eu.example.test/mesh".to_owned()],
            5_000,
            1_000,
            30_000,
        )
        .unwrap();
        let publisher = OperationsAssignmentPublisher::new(assignment_file.clone(), policy.clone());
        publisher
            .publish_committed(
                &OperationsCollectorAssignment {
                    schema_version: 1,
                    authority: "needletail-controller".to_owned(),
                    term: 8,
                    fencing_generation: 21,
                    leader_node_id: "collector-eu".to_owned(),
                    leader_region: "europe-west4".to_owned(),
                    quorum_healthy: true,
                    voters_online: 3,
                    voters_total: 5,
                    committed_at_unix_ms: now.saturating_sub(1_000),
                    lease_expires_unix_ms: now.saturating_add(10_000),
                    public_endpoint: "https://ops-eu.example.test/mesh".to_owned(),
                },
                now,
            )
            .unwrap();
        let state = AppState {
            assignment_file,
            policy,
        };

        let redirect = route_request(RequestRoute::Discover, &state);
        assert_eq!(redirect.status, 307);
        assert_eq!(
            redirect.location.as_deref(),
            Some("https://ops-eu.example.test/mesh")
        );

        publisher.withdraw().unwrap();
        let unavailable = route_request(RequestRoute::Discover, &state);
        assert_eq!(unavailable.status, 503);
        assert!(unavailable.location.is_none());

        fs::remove_dir_all(directory).unwrap();
    }
}
