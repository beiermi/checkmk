#!groovy

/// file: build-build-images.groovy

/// Build base images on top of (pinned) upstream OS images

/// Parameters / environment values:
///
/// Jenkins artifacts: ???
/// Other artifacts: ???
/// Depends on: image aliases for upstream OS images on Nexus, ???

def main() {
    check_job_parameters([
        "SYNC_WITH_IMAGES_WITH_UPSTREAM",
        "PUBLISH_IMAGES",
        "OVERRIDE_DISTROS",
        "BUILD_IMAGE_WITHOUT_CACHE",
    ]);

    check_environment_variables([
        "ARTIFACT_STORAGE",
        "NEXUS_ARCHIVES_URL",
    ]);

    def versioning = load("${checkout_dir}/buildscripts/scripts/utils/versioning.groovy");
    def all_distros = versioning.get_distros(override: "all")
    def distros = versioning.get_distros(edition: "all", use_case: "all", override: OVERRIDE_DISTROS);

    def vers_tag = versioning.get_docker_tag(scm, checkout_dir);
    def branch_name = versioning.safe_branch_name(scm);
    def branch_version = versioning.get_branch_version(checkout_dir);
    def publish_images = PUBLISH_IMAGES=='true';  // FIXME should be case sensitive

    print(
        """
        |===== CONFIGURATION ===============================
        |all_distros:....................(local)  │${all_distros}│
        |distros:........................(local)  │${distros}│
        |publish_images:.................(local)  │${publish_images}│
        |vers_tag:.......................(local)  │${vers_tag}│
        |branch_name:....................(local)  │${branch_name}│
        |branch_version:.................(local)  │${branch_version}│
        |===================================================
        """.stripMargin());

    currentBuild.description += (
        """
        |Building for the following Distros:
        |${distros}
        |""".stripMargin());


    withCredentials([
        usernamePassword(
            credentialsId: 'nexus',
            usernameVariable: 'NEXUS_USERNAME',
            passwordVariable: 'NEXUS_PASSWORD')
    ]) {
        def distro_base_image_id = [:];
        def real_distro_name = [:];

        stage("Provide\nupstream images") {
            dir("${checkout_dir}") {
                distro_base_image_id = distros.collectEntries { distro -> [
                    (distro) : {
                        real_distro_name[distro] = cmd_output(
                            "basename \$(realpath buildscripts/infrastructure/build-nodes/${distro})");
                        resolve_docker_image_alias(
                            "IMAGE_${real_distro_name[distro].toUpperCase().replaceAll('\\.', '_').replaceAll('-', '_')}")
                    }()
                ]};
                sh("""
                    cp defines.make static_variables.bzl package_versions.bzl .bazelversion omd/strip_binaries \
                    buildscripts/infrastructure/build-nodes/scripts

                    cp omd/distros/*.mk buildscripts/infrastructure/build-nodes/scripts
                """);
            }
        }

        // TODO: here it would be nice to iterate through all known distros
        //       and use a conditional_stage(distro in distros) approach
        def stages = all_distros.collectEntries { distro ->
            [("${distro}") : {
                    conditional_stage("Build\n${distro}", distro in distros) {
                        def image_name = "${distro}:${vers_tag}";
                        def distro_mk_file_name = "${real_distro_name[distro].toUpperCase().replaceAll('-', '_')}.mk";
                        def docker_build_args = (""
                            + " --build-arg DISTRO_IMAGE_BASE='${distro_base_image_id[distro]}'"
                            + " --build-arg DISTRO_MK_FILE='${distro_mk_file_name}'"
                            + " --build-arg DISTRO='${distro}'"
                            + " --build-arg VERS_TAG='${vers_tag}'"
                            + " --build-arg BRANCH_VERSION='${branch_version}'"

                            + " --build-arg DOCKER_REGISTRY='${docker_registry_no_http}'"
                            + " --build-arg NEXUS_ARCHIVES_URL='${NEXUS_ARCHIVES_URL}'"
                            + " --build-arg NEXUS_USERNAME='${NEXUS_USERNAME}'"
                            + " --build-arg NEXUS_PASSWORD='${NEXUS_PASSWORD}'"
                            + " --build-arg ARTIFACT_STORAGE='${ARTIFACT_STORAGE}'"
                            + " -f '${distro}/Dockerfile'"
                            + " ."
                        );

                        if (params.BUILD_IMAGE_WITHOUT_CACHE) {
                            docker_build_args = "--no-cache " + docker_build_args;
                        }
                        dir("${checkout_dir}/buildscripts/infrastructure/build-nodes") {
                            docker.build(image_name, docker_build_args);
                        }
                    }
                }
            ]
        }
        def images = parallel(stages);

        conditional_stage('upload images', publish_images) {
            docker.withRegistry(DOCKER_REGISTRY, "nexus") {
                images.each { distro, image ->
                    if (image) {
                        image.push();
                        image.push("${branch_name}-latest");
                    }
                }
            }
        }
    }
}

return this;
