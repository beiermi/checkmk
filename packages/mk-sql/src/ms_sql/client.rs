// Copyright (C) 2023 Checkmk GmbH - License: GNU General Public License v2
// This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
// conditions defined in the file COPYING, which is part of this source code package.

use crate::config::{self, ms_sql::AuthType, ms_sql::Endpoint};
use crate::types::{CertPath, HostName, Port};
use anyhow::Result;

#[cfg(windows)]
use crate::types::InstanceName; // only on windows possible to connect by name
#[cfg(windows)]
use tiberius::SqlBrowser;
use tiberius::{AuthMethod, Config};
use tokio::net::TcpStream;
use tokio_util::compat::{Compat, TokioAsyncWriteCompatExt};

use super::defaults;
pub type Client = tiberius::Client<Compat<TcpStream>>;

pub struct Remote<'a> {
    pub host: HostName,
    pub port: Option<Port>,
    pub credentials: Credentials<'a>,
}

#[cfg(windows)]
#[derive(Default)]
pub struct Local {
    pub port: Option<Port>,
}

#[cfg(windows)]
pub struct LocalInstance {
    pub instance_name: InstanceName,
    pub browser_port: Option<Port>,
}

enum ClientConnection<'a> {
    Remote(Remote<'a>),
    #[cfg(windows)]
    LocalInstance(LocalInstance),
    #[cfg(windows)]
    Local(Local),
}

pub struct ClientBuilder<'a> {
    client_connection: Option<ClientConnection<'a>>,

    database: Option<String>,
    certificate: Option<CertPath>,
    trust_server_certificate: bool,
}

impl<'a> Default for ClientBuilder<'a> {
    fn default() -> Self {
        Self {
            client_connection: None,
            database: None,
            certificate: None,
            trust_server_certificate: config::defines::defaults::TRUST_SERVER_CERTIFICATE,
        }
    }
}

impl<'a> ClientBuilder<'a> {
    pub fn new() -> ClientBuilder<'a> {
        ClientBuilder::default()
    }

    pub fn remote(
        mut self,
        host: &HostName,
        port: Option<Port>,
        credentials: Credentials<'a>,
    ) -> Self {
        let r = ClientConnection::Remote(Remote {
            host: host.to_owned(),
            port,
            credentials,
        });
        self.client_connection = Some(r);
        self
    }

    #[cfg(windows)]
    pub fn local_instance<P: Into<Port>>(
        mut self,
        instance: &InstanceName,
        browser_port: Option<P>,
    ) -> Self {
        let i = LocalInstance {
            instance_name: instance.to_owned(),
            browser_port: browser_port.map(|p| p.into()),
        };
        self.client_connection = Some(ClientConnection::LocalInstance(i));
        self
    }

    #[cfg(windows)]
    pub fn local(mut self, port: Option<Port>) -> Self {
        let l = ClientConnection::Local(Local { port });
        self.client_connection = Some(l);
        self
    }

    pub fn database<S: Into<String>>(mut self, database: Option<S>) -> Self {
        self.database = database.map(|d| d.into());
        self
    }

    pub fn certificate<C: Into<CertPath>>(mut self, certificate: Option<C>) -> Self {
        self.certificate = certificate.map(|c| c.into());
        self
    }

    pub fn trust_server_certificate(mut self, trust: bool) -> Self {
        self.trust_server_certificate = trust;
        self
    }

    pub fn make_config(&self) -> Result<Config> {
        let mut config = Config::new();

        match &self.client_connection {
            Some(ClientConnection::Remote(r)) => {
                let port = r.port.as_ref().map(|p| p.value());
                config.host(&r.host);
                config.port(port.unwrap_or(defaults::STANDARD_PORT));
                if let Some(db) = &self.database {
                    config.database(db);
                }
                config.authentication(match r.credentials {
                    Credentials::SqlServer { user, password } => {
                        AuthMethod::sql_server(user, password)
                    }
                    #[cfg(windows)]
                    Credentials::Windows { user, password } => AuthMethod::windows(user, password),
                    #[cfg(unix)]
                    Credentials::Windows {
                        user: _,
                        password: _,
                    } => anyhow::bail!("not supported"),
                });
            }
            #[cfg(windows)]
            Some(ClientConnection::LocalInstance(i)) => {
                let port = i.browser_port.as_ref().map(|p| p.value());
                config.host("localhost");
                config.port(port.unwrap_or(defaults::SQL_BROWSER_PORT));
                config.authentication(AuthMethod::Integrated);
                if let Some(db) = &self.database {
                    config.database(db);
                }
                config.instance_name(&i.instance_name);
            }
            #[cfg(windows)]
            Some(ClientConnection::Local(l)) => {
                let port = l.port.as_ref().map(|p| p.value());
                config.port(port.unwrap_or(defaults::STANDARD_PORT));
                config.authentication(AuthMethod::Integrated);
            }
            _ => anyhow::bail!("No client connection provided"),
        }
        if let Some(certificate) = &self.certificate {
            config.trust_cert_ca(certificate);
        } else if self.trust_server_certificate {
            config.trust_cert();
        }
        Ok(config)
    }

    pub async fn build(self) -> Result<Client> {
        let tiberius_config = self.make_config()?;
        match self.client_connection {
            Some(ClientConnection::Remote(_)) => create_remote(tiberius_config).await,
            #[cfg(windows)]
            Some(ClientConnection::LocalInstance(_)) => {
                create_instance_local(tiberius_config).await
            }
            #[cfg(windows)]
            Some(ClientConnection::Local(_)) => connect_via_tcp(tiberius_config).await,
            _ => anyhow::bail!("No client connection provided"),
        }
    }
}

pub enum Credentials<'a> {
    SqlServer { user: &'a str, password: &'a str },
    Windows { user: &'a str, password: &'a str },
}

pub const SQL_LOGIN_ERROR_TAG: &str = "[SQL LOGIN ERROR]";
pub const SQL_TCP_ERROR_TAG: &str = "[SQL TCP ERROR]";

pub async fn connect_main_endpoint(endpoint: &Endpoint) -> Result<Client> {
    connect_custom_endpoint(endpoint, endpoint.port()).await
}

pub async fn connect_custom_endpoint(endpoint: &Endpoint, port: Port) -> Result<Client> {
    let (auth, conn) = endpoint.split();
    let map_elapsed_to_anyhow = |e: tokio::time::error::Elapsed| {
        anyhow::anyhow!(
            "Timeout: {e} when creating client from config {:?}",
            conn.timeout()
        )
    };
    let client = match auth.auth_type() {
        AuthType::SqlServer | AuthType::Windows => {
            if let Some(credentials) = obtain_config_credentials(auth) {
                tokio::time::timeout(
                    conn.timeout(),
                    ClientBuilder::new()
                        .remote(conn.hostname(), Some(port), credentials)
                        .certificate(conn.tls().map(|t| t.client_certificate().to_owned()))
                        .trust_server_certificate(conn.trust_server_certificate())
                        .build(),
                )
                .await
                .map_err(map_elapsed_to_anyhow)?
            } else {
                anyhow::bail!("Not provided credentials")
            }
        }

        #[cfg(windows)]
        AuthType::Integrated => tokio::time::timeout(
            conn.timeout(),
            ClientBuilder::new()
                .local(Some(port))
                .certificate(conn.tls().map(|t| t.client_certificate().to_owned()))
                .trust_server_certificate(conn.trust_server_certificate())
                .build(),
        )
        .await
        .map_err(map_elapsed_to_anyhow)?,

        _ => anyhow::bail!("Not supported authorization type"),
    };

    client
}

pub fn obtain_config_credentials(auth: &config::ms_sql::Authentication) -> Option<Credentials> {
    match auth.auth_type() {
        AuthType::SqlServer => Some(Credentials::SqlServer {
            user: auth.username(),
            password: auth.password().map(|s| s.as_str()).unwrap_or(""),
        }),
        #[cfg(windows)]
        AuthType::Windows => Some(Credentials::Windows {
            user: auth.username(),
            password: auth.password().map(|s| s.as_str()).unwrap_or(""),
        }),
        _ => None,
    }
}

/// Create connection to remote MS SQL
///
/// # Arguments
///
/// * `host` - Hostname of MS SQL server
/// * `port` - Port of MS SQL server
/// * `credentials` - defines connection type and credentials itself
/// * `instance_name` - name of the instance to connect to
async fn create_remote(tiberius_config: Config) -> Result<Client> {
    let mut config = tiberius_config.clone();
    config.encryption(tiberius::EncryptionLevel::Required);
    match connect_via_tcp(config).await {
        Ok(client) => Ok(client),
        #[cfg(unix)]
        Err(err) => {
            log::warn!(
                "Encryption is not supported by the host, err is {}. Trying without encryption...",
                err
            );
            let mut config = tiberius_config.clone();
            config.encryption(tiberius::EncryptionLevel::NotSupported);
            Ok(connect_via_tcp(config).await?)
        }
        #[cfg(windows)]
        Err(err) => {
            log::warn!(
                "Encryption is not supported by the host, err is {}. Stopping...",
                err
            );
            Err(err)
        }
    }
}

/// Create `local` connection to MS SQL `instance`
///
/// # Arguments
///
/// * `instance_name` - name of the instance to connect to
/// * `port` - Port of MS SQL server BROWSER,  1434 - default
#[cfg(windows)]
async fn create_instance_local(config: Config) -> anyhow::Result<Client> {
    log::info!("Connection to addr {}", config.get_addr());
    // This will create a new `TcpStream` from `async-std`, connected to the
    // right port of the named instance.
    // The logic is based on SQL browser mechanic
    let tcp = TcpStream::connect_named(&config)
        .await
        .map_err(|e| anyhow::anyhow!("{} {}", SQL_TCP_ERROR_TAG, e))?;

    // And from here on continue the connection process in a normal way.
    let s = Client::connect(config, tcp.compat_write())
        .await
        .map_err(|e| anyhow::anyhow!("{} {}", SQL_LOGIN_ERROR_TAG, e))?;
    Ok(s)
}

async fn connect_via_tcp(config: Config) -> Result<Client> {
    log::info!("Connection to addr {}", config.get_addr());
    let tcp = TcpStream::connect(config.get_addr()).await.map_err(|e| {
        anyhow::anyhow!(
            "{} address:{} error:`{}`",
            SQL_TCP_ERROR_TAG,
            config.get_addr(),
            e
        )
    })?;
    tcp.set_nodelay(true)?;

    // To be able to use Tokio's tcp, we're using the `compat_write` from
    // the `TokioAsyncWriteCompatExt` to get a stream compatible with the
    // traits from the `futures` crate. The same is for upcoming NamedPipe
    Client::connect(config, tcp.compat_write())
        .await
        .map_err(|e| anyhow::anyhow!("{} {}", SQL_LOGIN_ERROR_TAG, e))
}

/// Create `local` connection to MS SQL `instance`
///
/// # Arguments
///
/// * `port` - Port of MS SQL server BROWSER,  1434 - default
/// * `instance_name` - name of the instance to connect to
#[cfg(unix)]
pub async fn create_instance_local(
    _instance_name: &str,
    _port: Option<u16>,
    _database: Option<String>,
) -> anyhow::Result<Client> {
    anyhow::bail!("not supported");
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::ms_sql::Config;

    fn make_config_with_auth_type(auth_type: &str) -> Config {
        const BASE: &str = r#"
---
mssql:
  main:
    authentication:
       username: "bad_user"
       password: "bad_password"
       type: type_tag
    connection:
       hostname: "localhost" # we use real host to avoid long timeout
       port: 65345 # we use weird port to avoid connection
       timeout: 1
"#;
        Config::from_string(&BASE.replace("type_tag", auth_type))
            .unwrap()
            .unwrap()
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn test_create_client_from_config_for_error() {
        let config = make_config_with_auth_type("token");
        assert!(connect_main_endpoint(&config.endpoint())
            .await
            .unwrap_err()
            .to_string()
            .contains("Not supported authorization type"));
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn test_create_client_from_config_timeout() {
        let config = make_config_with_auth_type("sql_server");
        let s = connect_main_endpoint(&config.endpoint())
            .await
            .unwrap_err()
            .to_string();
        // in Windows connection is slow enough, we could verify timeout
        #[cfg(windows)]
        assert!(s.contains("Timeout: "), "{s}");

        // in linux connection is too fast, no chance for timeout check
        #[cfg(unix)]
        assert!(s.contains("Connection refused"), "{s}");
    }

    #[test]
    fn test_obtain_credentials_from_config() {
        #[cfg(windows)]
        assert!(obtain_config_credentials(make_config_with_auth_type("windows").auth()).is_some());
        assert!(
            obtain_config_credentials(make_config_with_auth_type("sql_server").auth()).is_some()
        );
    }

    #[cfg(windows)]
    #[tokio::test(flavor = "multi_thread")]
    async fn test_local_with_cert() {
        pub const MS_SQL_DB_CERT: &str = "CI_TEST_MS_SQL_DB_CERT";
        if let Ok(certificate_path) = std::env::var(MS_SQL_DB_CERT) {
            ClientBuilder::new()
                .local(Some(1433u16.into()))
                .certificate(Some(certificate_path))
                .trust_server_certificate(false)
                .build()
                .await
                .unwrap();
            assert!(ClientBuilder::new()
                .local(Some(1433u16.into()))
                .trust_server_certificate(false)
                .build()
                .await
                .is_err());
            assert!(ClientBuilder::new()
                .local(Some(1433u16.into()))
                .trust_server_certificate(true)
                .build()
                .await
                .is_ok());
        } else {
            eprintln!("Error: environment variable {} is absent", MS_SQL_DB_CERT);
        }
    }

    #[test]
    fn test_client_builder_remote() {
        let credentials = Credentials::SqlServer {
            user: "u",
            password: "p",
        };
        let remote = ClientBuilder::new();
        assert!(remote.client_connection.is_none());
        let host: HostName = "host".to_owned().into();
        let port: Option<Port> = Some(123u16.into());
        let builder = remote.remote(&host, port, credentials);
        assert!(matches!(
            builder.client_connection,
            Some(ClientConnection::Remote(Remote {
                host: _,
                port: _,
                credentials: _
            }))
        ));
    }
    #[cfg(windows)]
    #[test]
    fn test_client_builder_local_instance() {
        let local = ClientBuilder::new();
        let instance_name: InstanceName = "i".to_owned().into();
        let browser_port: Option<Port> = Some(123u16.into());
        let builder = local.local_instance(&instance_name, browser_port);
        assert!(matches!(
            builder.client_connection,
            Some(ClientConnection::LocalInstance(LocalInstance {
                instance_name: _,
                browser_port: _,
            }))
        ));
    }
    #[cfg(windows)]
    #[test]
    fn test_client_builder_local() {
        let local = ClientBuilder::new();
        let port: Option<Port> = Some(123u16.into());
        let builder = local.local(port);
        assert!(matches!(
            builder.client_connection,
            Some(ClientConnection::Local(Local { port: _ }))
        ));
    }
}
