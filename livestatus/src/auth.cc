// Copyright (C) 2019 tribe29 GmbH - License: GNU General Public License v2
// This file is part of Checkmk (https://checkmk.com). It is subject to the
// terms and conditions defined in the file COPYING, which is part of this
// source code package.

#include "auth.h"

#include <algorithm>
#include <vector>

#include "StringUtils.h"

#ifdef CMC
#include "ContactGroup.h"
#include "Host.h"         // IWYU pragma: keep
#include "ObjectGroup.h"  // IWYU pragma: keep
#include "Service.h"      // IWYU pragma: keep
#include "World.h"
#include "cmc.h"
#endif

namespace {
bool host_has_contact(const host *hst, const contact *ctc) {
#ifdef CMC
    return hst->hasContact(ctc);
#else
    // Older Nagios headers are not const-correct... :-P
    return is_contact_for_host(const_cast<host *>(hst),
                               const_cast<contact *>(ctc)) != 0 ||
           is_escalated_contact_for_host(const_cast<host *>(hst),
                                         const_cast<contact *>(ctc)) != 0;
#endif
}

bool service_has_contact(const service *svc, const contact *ctc) {
#ifdef CMC
    return svc->hasContact(ctc);
#else
    // Older Nagios headers are not const-correct... :-P
    return is_contact_for_service(const_cast<service *>(svc),
                                  const_cast<contact *>(ctc)) != 0 ||
           is_escalated_contact_for_service(const_cast<service *>(svc),
                                            const_cast<contact *>(ctc)) != 0;
#endif
}

const host *host_for_service(const service *svc) {
#ifdef CMC
    return svc->host();
#else
    return svc->host_ptr;
#endif
}

bool is_authorized_for_hst(const contact *ctc, const host *hst) {
    if (ctc == no_auth_user()) {
        return true;
    }
    if (ctc == unknown_auth_user()) {
        return false;
    }
    return host_has_contact(hst, ctc);
}

bool is_authorized_for_svc(ServiceAuthorization service_auth,
                           const contact *ctc, const service *svc) {
    if (ctc == no_auth_user()) {
        return true;
    }
    if (ctc == unknown_auth_user()) {
        return false;
    }
    return service_has_contact(svc, ctc) ||
           (service_auth == ServiceAuthorization::loose &&
            host_has_contact(host_for_service(svc), ctc));
}

bool is_authorized_for_host_group(GroupAuthorization group_auth,
                                  const hostgroup *hg, const contact *ctc) {
    if (ctc == no_auth_user()) {
        return true;
    }
    if (ctc == unknown_auth_user()) {
        return false;
    }
    auto is_authorized_for = [=](const host *hst) {
        return is_authorized_for_hst(ctc, hst);
    };
#ifdef CMC
    return group_auth == GroupAuthorization::loose
               ? std::any_of(hg->begin(), hg->end(), is_authorized_for)
               : std::all_of(hg->begin(), hg->end(), is_authorized_for);
#else
    if (group_auth == GroupAuthorization::loose) {
        for (hostsmember *mem = hg->members; mem != nullptr; mem = mem->next) {
            if (is_authorized_for(mem->host_ptr)) {
                return true;
            }
        }
        return false;
    }
    for (hostsmember *mem = hg->members; mem != nullptr; mem = mem->next) {
        if (!is_authorized_for(mem->host_ptr)) {
            return false;
        }
    }
    return true;
#endif
}

bool is_authorized_for_service_group(GroupAuthorization group_auth,
                                     ServiceAuthorization service_auth,
                                     const servicegroup *sg,
                                     const contact *ctc) {
    if (ctc == no_auth_user()) {
        return true;
    }
    if (ctc == unknown_auth_user()) {
        return false;
    }
    auto is_authorized_for = [=](const service *svc) {
        return is_authorized_for_svc(service_auth, ctc, svc);
    };
#ifdef CMC
    return group_auth == GroupAuthorization::loose
               ? std::any_of(sg->begin(), sg->end(), is_authorized_for)
               : std::all_of(sg->begin(), sg->end(), is_authorized_for);
#else
    if (group_auth == GroupAuthorization::loose) {
        for (const auto *mem = sg->members; mem != nullptr; mem = mem->next) {
            if (is_authorized_for(mem->service_ptr)) {
                return true;
            }
        }
        return false;
    }
    for (const auto *mem = sg->members; mem != nullptr; mem = mem->next) {
        if (!is_authorized_for(mem->service_ptr)) {
            return false;
        }
    }
    return true;
#endif
}

bool is_member_of_contactgroup(const std::string &group,
                               const contact *contact) {
#ifdef CMC
    const auto *cg = g_live_world->getContactGroup(group);
    return cg != nullptr && cg->isMember(contact);
#else
    // Older Nagios headers are not const-correct... :-P
    return ::is_contact_member_of_contactgroup(
               ::find_contactgroup(const_cast<char *>(group.c_str())),
               const_cast< ::contact *>(contact)) != 0;
#endif
}
}  // namespace

User::User(const contact *auth_user, ServiceAuthorization service_auth,
           GroupAuthorization group_auth)
    : auth_user_{auth_user}
    , service_auth_{service_auth}
    , group_auth_{group_auth} {}

bool User::is_authorized_for_everything() const {
    return auth_user_ == no_auth_user();
}

bool User::is_authorized_for_host(const host &hst) const {
    return ::is_authorized_for_hst(auth_user_, &hst);
}

bool User::is_authorized_for_service(const service &svc) const {
    return ::is_authorized_for_svc(service_auth_, auth_user_, &svc);
}

bool User::is_authorized_for_host_group(const hostgroup &hg) const {
    return ::is_authorized_for_host_group(group_auth_, &hg, auth_user_);
}

bool User::is_authorized_for_service_group(const servicegroup &sg) const {
    return ::is_authorized_for_service_group(group_auth_, service_auth_, &sg,
                                             auth_user_);
}

namespace mk::ec {
// The funny encoding of an Optional[Iterable[str]] is done in
// cmk.ec.history.quote_tab().

bool is_none(const std::string &str) { return str == "\002"; }

std::vector<std::string> split_list(const std::string &str) {
    return str.empty() || is_none(str) ? std::vector<std::string>()
                                       : mk::split(str.substr(1), '\001');
}
}  // namespace mk::ec

bool User::is_authorized_for_event(const std::string &precedence,
                                   const std::string &contact_groups,
                                   const host *hst) const {
    if (auth_user_ == no_auth_user()) {
        return true;
    }
    if (auth_user_ == unknown_auth_user()) {
        return false;
    }

    auto is_member = [this](const auto &group) {
        return is_member_of_contactgroup(group, auth_user_);
    };
    auto is_authorized_via_contactgroups = [is_member, &contact_groups]() {
        auto groups{mk::ec::split_list(contact_groups)};
        return std::any_of(groups.begin(), groups.end(), is_member);
    };

    if (precedence == "rule") {
        if (!mk::ec::is_none(contact_groups)) {
            return is_authorized_via_contactgroups();
        }
        if (hst != nullptr) {
            return is_authorized_for_host(*hst);
        }
        return true;
    }
    if (precedence == "host") {
        if (hst != nullptr) {
            return is_authorized_for_host(*hst);
        }
        if (!mk::ec::is_none(contact_groups)) {
            return is_authorized_via_contactgroups();
        }
        return true;
    }
    return false;
}
