class openstack_project::etherpad_dev (
  $mysql_password,
  $mysql_host = 'localhost',
  $mysql_user = 'eplite',
  $mysql_db_name = 'etherpad-lite'
) {
  class { 'etherpad_lite':
    ep_ensure      => 'latest',
    nodejs_version => 'system',
  }

  class { 'etherpad_lite::apache':
    ssl_cert_file  => '/etc/ssl/certs/ssl-cert-snakeoil.pem',
    ssl_key_file   => '/etc/ssl/private/ssl-cert-snakeoil.key',
    ssl_chain_file => '',
  }

  class { 'etherpad_lite::site':
    etherpad_title    => 'OpenStack Dev Etherpad',
    database_host     => $mysql_host,
    database_user     => $mysql_user,
    database_name     => $mysql_db_name,
    database_password => $mysql_password,
  }

  etherpad_lite::plugin { 'ep_headings':
    require => Class['etherpad_lite'],
  }

  mysql_backup::backup_remote { 'etherpad-lite-dev':
    database_host     => $mysql_host,
    database_user     => $mysql_user,
    database_password => $mysql_password,
    require           => Class['etherpad_lite'],
  }
}

# vim:sw=2:ts=2:expandtab:textwidth=79
